# COA production rollout through the server-side NAS mapping

This runbook enables the existing deterministic COA workflow on the verified
direct deployment at `vmd104084.contaboserver.net`. It does not use Docker.

Production layout:

- checkout: `/opt/aiemail`
- virtual environment: `/opt/aiemail-env`
- systemd environment: `/etc/aiemail/aiemail.env`
- services that use COA data: `aiemail-api` and `aiemail-worker`
- service account: `aiemail`
- local API: `http://127.0.0.1:8000`

The Synology mapping must appear to the application as a local Linux path. The
examples below use `/mnt/lanyachem`; replace it with the already configured live
mount point if it differs. Keep the mapping read-only. Store SMB credentials in
a root-owned system file, never in this repository or the application
environment file.

Never run `source /etc/aiemail/aiemail.env`; some values contain spaces. Never
print that file wholesale because it contains secrets. Do not use `git clean`;
production contains intentional untracked files.

## 1. Verify the live mapping without changing the application

Run these checks before editing configuration:

```bash
COA_MOUNT=/mnt/lanyachem
COA_ROOT="$COA_MOUNT/!PRODUCT DATA/!PRODUCT DOCS"

findmnt -T "$COA_ROOT" -o TARGET,SOURCE,FSTYPE,OPTIONS
mountpoint -q "$COA_MOUNT"
sudo -u aiemail test -d "$COA_ROOT"
sudo -u aiemail test -r "$COA_ROOT"
sudo -u aiemail find "$COA_ROOT" -type f -iname '*coa*.pdf' -print -quit \
  | grep -q .
```

All commands must succeed. Confirm that the `findmnt` options include `ro` and
that the mapping is persistent across reboot. If the mount is unavailable or
the `aiemail` user cannot traverse/read it, stop here; do not make the
application run as root and do not enable the COA workflow.

Also inventory the current application state without exposing secrets:

```bash
cd /opt/aiemail
git status --short
git rev-parse --short HEAD
systemctl is-active aiemail-api aiemail-worker aiemail-imap
curl -fsS http://127.0.0.1:8000/health

grep -E \
'^(AUTO_SEND_ENABLED|SAFE_MODE|COA_CATALOG_ENABLED|COA_CATALOG_ROOT|COA_CATALOG_PATH|COA_PRODUCT_CATALOG_PATH|COA_CATALOG_POLL_SECONDS|COA_AUTO_SEND_ENABLED)=' \
/etc/aiemail/aiemail.env
```

Stop if pending deployment files conflict with an intentional untracked
production file. A missing COA setting is acceptable before first activation.

## 2. Build and inspect the catalog as the service account

Create only the application-owned output directory, then perform one foreground
scan against the read-only mapping:

```bash
COA_ROOT="/mnt/lanyachem/!PRODUCT DATA/!PRODUCT DOCS"

install -d -o aiemail -g aiemail /opt/aiemail/runtime/coa_catalog

sudo -u aiemail env PYTHONNOUSERSITE=1 \
  /opt/aiemail-env/bin/python \
  /opt/aiemail/scripts/sync_coa_catalog.py \
  --root "$COA_ROOT" \
  --output /opt/aiemail/runtime/coa_catalog/catalog.json \
  --product-catalog /opt/aiemail/config/product_catalog.yaml
```

The summary must report:

- `complete: true`;
- `enumeration_warning_count: 0`;
- `selected_count` greater than zero for a usable catalog;
- `extraction_error_count: 0` before autonomous sending is considered.

`review_count` may be nonzero. Those ambiguous, customer-specific, dated,
versioned, Chinese-path, or otherwise nonstandard candidates are deliberately
excluded and require human correction; the system never guesses among them.

## 3. Enable reviewed COA drafts first

Back up the environment file, then edit only the following keys. Quoting the
root value makes the embedded spaces explicit to systemd:

```bash
AIEMAIL_COA_TS=$(date +%Y%m%d-%H%M%S)
mkdir -p /root/aiemail-backups
cp -a /etc/aiemail/aiemail.env \
  "/root/aiemail-backups/aiemail.env.before-coa-$AIEMAIL_COA_TS"
```

Set these values in `/etc/aiemail/aiemail.env` without loading it in a shell:

```dotenv
COA_CATALOG_ENABLED=true
COA_CATALOG_ROOT="/mnt/lanyachem/!PRODUCT DATA/!PRODUCT DOCS"
COA_CATALOG_PATH=/opt/aiemail/runtime/coa_catalog/catalog.json
COA_PRODUCT_CATALOG_PATH=/opt/aiemail/config/product_catalog.yaml
COA_CATALOG_SCAN_ENABLED=false
COA_CATALOG_POLL_SECONDS=300
COA_CATALOG_MAX_FILE_MB=50
COA_CATALOG_FILE_TIMEOUT_SECONDS=15
COA_AUTO_SEND_ENABLED=false
```

Preserve the environment file permissions:

```bash
chown root:aiemail /etc/aiemail/aiemail.env
chmod 640 /etc/aiemail/aiemail.env
systemctl restart aiemail-api aiemail-worker
systemctl is-active aiemail-api aiemail-worker
curl -fsS http://127.0.0.1:8000/health
journalctl -u aiemail-worker --since '10 minutes ago' --no-pager -l
```

With `COA_AUTO_SEND_ENABLED=false`, an exact verified match creates an editable
`COA_REVIEW` draft. Approval through the protected handoff page reopens the
exact NAS file, checks that its SHA-256 still matches the catalog, embeds it as
a PDF attachment, and only then queues the customer email.

Use the authenticated administration endpoints to verify the live state:

- `GET /admin/coa/status` — scan completion, counts, warnings, root and poll
  configuration;
- `GET /admin/coa/find?query=PRODUCT_CODE` — deterministic test lookup;
- `GET /admin/coa/review` — excluded or ambiguous candidates;
- `POST /admin/coa/scan` — on-demand refresh after a controlled file update.

Test with a designated internal mailbox before approving a real customer
message. Verify the received PDF filename and content, not only that the email
was delivered.

## 4. Optional bounded autonomous sending

Do not enable autonomous COA replies merely because the mount is reachable.
First review real draft-only results and catalog exceptions. When approved for
production, autonomous delivery additionally requires the global send policy,
the customer's `auto_send_allowed` state, confidence thresholds, suppression
checks, and an exact eligible catalog match.

Change only this workflow switch when those gates have been accepted:

```dotenv
COA_AUTO_SEND_ENABLED=true
```

Restart `aiemail-api` and `aiemail-worker`, then monitor both the worker journal
and Outbox. Leave `PRODUCT_LIST_AUTO_SEND_ENABLED` and
`QUOTE_AUTO_SEND_ENABLED` unchanged; COA activation does not authorize either
workflow.

## 5. Failure and rollback behavior

The workflow is fail-closed. An unreadable mount, missing catalog, ambiguous
match, changed hash, oversized file, or extraction error must create/retain a
review requirement instead of sending an attachment. Non-COA workflows remain
available while the NAS mapping is down.

Immediate kill switch:

```dotenv
COA_AUTO_SEND_ENABLED=false
COA_CATALOG_ENABLED=false
```

After editing, restart `aiemail-api` and `aiemail-worker`. To restore the exact
previous settings, copy back the timestamped environment backup, restore
`root:aiemail` ownership and mode `640`, and restart the same two services. Do
not delete the catalog or any NAS files during rollback.
