# Inbound disposition production rollout

This runbook applies to the verified direct deployment on
`vmd104084.contaboserver.net`. It does not use Docker.

Production layout:

- checkout: `/opt/aiemail`
- virtual environment: `/opt/aiemail-env`
- systemd environment: `/etc/aiemail/aiemail.env`
- services: `aiemail-api`, `aiemail-worker`, `aiemail-imap`
- local API: `http://127.0.0.1:8000`
- database administration variables: `/root/aiemail-db.env`

Never run `source /etc/aiemail/aiemail.env`; some values contain spaces. Do not
use `git clean`, because production contains intentional untracked files.

## 1. Read-only inventory

Run this before stopping anything:

```bash
cd /opt/aiemail
git status --short
git rev-parse --short HEAD
git fetch origin main
git rev-parse --short FETCH_HEAD
git log --oneline HEAD..FETCH_HEAD

systemctl is-active aiemail-api aiemail-worker aiemail-imap
curl -fsS http://127.0.0.1:8000/health

grep -E \
'^(AUTO_SEND_ENABLED|IMAP_SYNC_ENABLED|INBOUND_DISPOSITION_ENABLED|INBOUND_DISPOSITION_AI_ENABLED|INBOUND_DISPOSITION_AI_MIN_CONFIDENCE|INBOUND_DISPOSITION_AI_BATCH_ENABLED|INBOUND_DISPOSITION_AI_MAX_BATCH|INBOUND_DISPOSITION_AI_BATCH_POLL_SECONDS|INBOUND_DISPOSITION_AI_BATCH_MAX_ATTEMPTS|INBOUND_DISPOSITION_APPLY_ENABLED|REFERRAL_AUTO_CONTACT_ENABLED)=' \
/etc/aiemail/aiemail.env
```

Load only the dedicated database administration file and inventory mutable work:

```bash
set -a
source /root/aiemail-db.env
set +a

PGPASSWORD="$POSTGRES_PASSWORD" /usr/pgsql-15/bin/psql \
  -h 127.0.0.1 \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  -P pager=off \
  -c "SELECT version_num FROM alembic_version;
      SELECT message_kind, status, count(*)
      FROM outbox
      WHERE status IN ('PENDING','CLAIMED','FAILED','UNKNOWN')
      GROUP BY message_kind, status
      ORDER BY message_kind, status;
      SELECT kind, status, count(*)
      FROM jobs
      WHERE status IN ('PENDING','RUNNING','FAILED')
      GROUP BY kind, status
      ORDER BY kind, status;"
```

Stop if an Outbox row is `CLAIMED` or `UNKNOWN`, or if `git status --short`
shows a path that the incoming commit also creates.

## 2. Stop and back up

```bash
systemctl stop aiemail-imap aiemail-worker aiemail-api
systemctl is-active aiemail-api aiemail-worker aiemail-imap

AIEMAIL_DEPLOY_TS=$(date +%Y%m%d-%H%M%S)
mkdir -p /root/aiemail-backups
cp -a /etc/aiemail/aiemail.env \
  "/root/aiemail-backups/aiemail.env.before-inbound-disposition-$AIEMAIL_DEPLOY_TS"

AIEMAIL_BACKUP_FILE="/root/aiemail-backups/pre-inbound-disposition-$AIEMAIL_DEPLOY_TS.dump"
PGPASSWORD="$POSTGRES_PASSWORD" /usr/pgsql-15/bin/pg_dump \
  -h 127.0.0.1 \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  -Fc \
  -f "$AIEMAIL_BACKUP_FILE"

test -s "$AIEMAIL_BACKUP_FILE"
ls -lh \
  "$AIEMAIL_BACKUP_FILE" \
  "/root/aiemail-backups/aiemail.env.before-inbound-disposition-$AIEMAIL_DEPLOY_TS"
```

Record the rollback revision before pulling:

```bash
AIEMAIL_PREVIOUS_COMMIT=$(git rev-parse HEAD)
printf '%s\n' "$AIEMAIL_PREVIOUS_COMMIT" \
  > "/root/aiemail-backups/pre-inbound-disposition-$AIEMAIL_DEPLOY_TS.commit"
```

## 3. Pull and install

Only continue after confirming that `FETCH_HEAD` is the reviewed release commit.

```bash
git pull --ff-only origin main
git rev-parse --short HEAD
git status --short

/opt/aiemail-env/bin/python -m pip install -e .

sudo -u aiemail env PYTHONNOUSERSITE=1 \
  /opt/aiemail-env/bin/python -c \
  'import app.main, app.disposition_service, app.inbound_disposition; print("imports: ok")'
```

Untracked production files should still appear. That is expected; do not delete
them.

## 4. Force observation-mode switches

Use this helper to update only the named lines without loading or printing the
environment file:

```bash
set_aiemail_flag() {
  key="$1"
  value="$2"
  file=/etc/aiemail/aiemail.env
  if grep -q "^${key}=" "$file"; then
    sed -i "s/^${key}=.*/${key}=${value}/" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

set_aiemail_flag INBOUND_DISPOSITION_ENABLED true
set_aiemail_flag INBOUND_DISPOSITION_AI_ENABLED true
set_aiemail_flag INBOUND_DISPOSITION_AI_MIN_CONFIDENCE 0.80
set_aiemail_flag INBOUND_DISPOSITION_AI_BATCH_ENABLED true
set_aiemail_flag INBOUND_DISPOSITION_AI_MAX_BATCH 250
set_aiemail_flag INBOUND_DISPOSITION_AI_BATCH_POLL_SECONDS 20
set_aiemail_flag INBOUND_DISPOSITION_AI_BATCH_MAX_ATTEMPTS 3
set_aiemail_flag INBOUND_DISPOSITION_APPLY_ENABLED false
set_aiemail_flag REFERRAL_AUTO_CONTACT_ENABLED false

chown root:aiemail /etc/aiemail/aiemail.env
chmod 640 /etc/aiemail/aiemail.env

grep -E \
'^(INBOUND_DISPOSITION_ENABLED|INBOUND_DISPOSITION_AI_ENABLED|INBOUND_DISPOSITION_AI_MIN_CONFIDENCE|INBOUND_DISPOSITION_AI_BATCH_ENABLED|INBOUND_DISPOSITION_AI_MAX_BATCH|INBOUND_DISPOSITION_AI_BATCH_POLL_SECONDS|INBOUND_DISPOSITION_AI_BATCH_MAX_ATTEMPTS|INBOUND_DISPOSITION_APPLY_ENABLED|REFERRAL_AUTO_CONTACT_ENABLED)=' \
/etc/aiemail/aiemail.env
```

Expected output:

```text
INBOUND_DISPOSITION_ENABLED=true
INBOUND_DISPOSITION_AI_ENABLED=true
INBOUND_DISPOSITION_AI_MIN_CONFIDENCE=0.80
INBOUND_DISPOSITION_AI_BATCH_ENABLED=true
INBOUND_DISPOSITION_AI_MAX_BATCH=250
INBOUND_DISPOSITION_AI_BATCH_POLL_SECONDS=20
INBOUND_DISPOSITION_AI_BATCH_MAX_ATTEMPTS=3
INBOUND_DISPOSITION_APPLY_ENABLED=false
REFERRAL_AUTO_CONTACT_ENABLED=false
```

## 5. Migrate and start

The API unit runs `alembic upgrade head` in `ExecStartPre`.

```bash
systemctl start aiemail-api
systemctl status aiemail-api --no-pager -l
journalctl -u aiemail-api --since "5 minutes ago" --no-pager -l

curl -fsS http://127.0.0.1:8000/health

PGPASSWORD="$POSTGRES_PASSWORD" /usr/pgsql-15/bin/psql \
  -h 127.0.0.1 \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  -P pager=off \
  -c "SELECT version_num FROM alembic_version;"
```

The expected revision is `0023`. Start the background services only after API
health and migration checks pass:

```bash
systemctl start aiemail-worker aiemail-imap
systemctl is-active aiemail-api aiemail-worker aiemail-imap
journalctl \
  -u aiemail-worker \
  -u aiemail-imap \
  --since "5 minutes ago" \
  --no-pager \
  -l
```

## 6. Real-mail dry-run

Open the protected page:

```text
https://aiemail.lanyachem.de/admin/inbound-dispositions
```

Start with 500 recent live messages, ordinary business mail excluded and synced
history excluded. Then repeat with synced history included. Bulk scans are fixed
to dry-run and cannot write CRM state or create Outbox rows.

For shell-based capture, enter the administrator credentials interactively so
they do not appear in shell history:

```bash
read -r -p "Admin user: " AIEMAIL_ADMIN_USER
read -r -s -p "Admin password: " AIEMAIL_ADMIN_PASSWORD
echo

AIEMAIL_DRY_RUN_START_FILE="/root/aiemail-backups/inbound-disposition-dry-run-start-$AIEMAIL_DEPLOY_TS.json"
AIEMAIL_DRY_RUN_FILE="/root/aiemail-backups/inbound-disposition-dry-run-final-$AIEMAIL_DEPLOY_TS.json"
curl -fsS \
  -u "$AIEMAIL_ADMIN_USER:$AIEMAIL_ADMIN_PASSWORD" \
  -X POST \
  "http://127.0.0.1:8000/admin/inbound-dispositions/backfill?limit=1000&include_business=false&include_synced_history=false" \
  -o "$AIEMAIL_DRY_RUN_START_FILE"
chmod 600 "$AIEMAIL_DRY_RUN_START_FILE"

AIEMAIL_BATCH_ID=$(
  /opt/aiemail-env/bin/python -c \
  'import json,sys; print(int(json.load(open(sys.argv[1],encoding="utf-8"))["batch_id"]))' \
  "$AIEMAIL_DRY_RUN_START_FILE"
)

for attempt in $(seq 1 90); do
  curl -fsS \
    -u "$AIEMAIL_ADMIN_USER:$AIEMAIL_ADMIN_PASSWORD" \
    "http://127.0.0.1:8000/admin/inbound-dispositions/batches/$AIEMAIL_BATCH_ID" \
    -o "$AIEMAIL_DRY_RUN_FILE"
  chmod 600 "$AIEMAIL_DRY_RUN_FILE"
  if /opt/aiemail-env/bin/python -c \
    'import json,sys; raise SystemExit(0 if json.load(open(sys.argv[1],encoding="utf-8")).get("complete") else 1)' \
    "$AIEMAIL_DRY_RUN_FILE"; then
    break
  fi
  sleep 5
done

/opt/aiemail-env/bin/python -c \
'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print({k:d[k] for k in ("mode","batch_id","batch_status","complete","scanned_count","candidate_count","applied_count","counts","ai_summary")})' \
"$AIEMAIL_DRY_RUN_FILE"

unset AIEMAIL_ADMIN_PASSWORD
```

The final response must report `mode: batch-dry-run`, `complete: true`, a
terminal `batch_status`, and `applied_count: 0`. The first POST normally shows
only deterministic rule results while the AI items are still pending; it is not
the final audit result. The protected page lists the most recent 50 batches, so
operators can switch batches without editing the URL. Historical batches are
read-only in both the browser and the apply endpoint.

Also prove that the dry-run created no referral mail:

```bash
PGPASSWORD="$POSTGRES_PASSWORD" /usr/pgsql-15/bin/psql \
  -h 127.0.0.1 \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  -P pager=off \
  -c "SELECT message_kind, status, count(*)
      FROM outbox
      WHERE message_kind = 'REFERRAL_OUTREACH'
      GROUP BY message_kind, status;
      SELECT count(*) AS applied_actions
      FROM inbound_disposition_actions
      WHERE status = 'APPLIED';"
```

## 7. Review requirements before automatic mutation

Do not enable automatic mutation until representative real samples prove all of
the following:

- human replies mentioning somebody else's departure keep the sender active and
  continue product-list handling;
- a verified changed sender retires only the original historical endpoint;
- out-of-office dates are correct, and uncertain dates show a blocker;
- forwarded-to-colleague mail does not queue duplicate outreach;
- logistics providers and suppliers offering prices to Lanya are marked only
  when newly authored text contains an explicit role or offer signal;
- temporary-absence messages with a backup address remain
  `TEMPORARY_ABSENCE` while preserving the referral;
- a referral without a valid authored-body email becomes `UNCERTAIN` and cannot
  modify CRM data;
- an explicit "there is no [name] in our company" response becomes
  `CONTACT_IDENTITY_MISMATCH`; it creates human review and never marks the whole
  customer `NON_TARGET`;
- signature and quoted-history addresses are not treated as replacements;
- all multi-address, cross-domain, missing-customer, and ambiguous records show
  blockers;
- one manually confirmed action can be safely rolled back from the page.

For the observed boundary samples, require these results in two newly created
batches before enabling mutation:

- email `#2090`: `NON_TARGET` with reason `SUPPLIER_VENDOR`;
- email `#2566`: `TEMPORARY_ABSENCE`, preserving `ps@vipullife.com`;
- email `#2981`: `CONTACT_IDENTITY_MISMATCH`, never `CONTACT_REFERRAL` or
  customer-level `NON_TARGET`;
- email `#4014`: `TEMPORARY_ABSENCE`, preserving
  `jmstraley@bouldersci.com`.

AI confidence alone is not an acceptance criterion. Confirm the normalized
category, extracted address, blockers, and proposed actions. Any exhausted AI
item must remain visibly marked for attention and must not be treated as a
successful semantic decision.

The first release step, after that review, is CRM mutation only:

```text
INBOUND_DISPOSITION_APPLY_ENABLED=true
REFERRAL_AUTO_CONTACT_ENABLED=false
```

Restart all three units and re-check health after changing a switch. Keep
referral outreach disabled for a separate observation period.

`REFERRAL_AUTO_CONTACT_ENABLED=true` is an independent sending authorization.
When it and `AUTO_SEND_ENABLED=true` are both enabled, an explicitly queued
referral message may send immediately. Enable it only after reviewing the exact
draft and recipient behavior.

## 8. Application rollback

The `0022` and `0023` migrations are additive. For an emergency application rollback, keep
the database schema in place so audit and referral data are preserved:

```bash
systemctl stop aiemail-imap aiemail-worker aiemail-api
cd /opt/aiemail

AIEMAIL_PREVIOUS_COMMIT=$(cat "/root/aiemail-backups/pre-inbound-disposition-$AIEMAIL_DEPLOY_TS.commit")
git switch --detach "$AIEMAIL_PREVIOUS_COMMIT"
/opt/aiemail-env/bin/python -m pip install -e .

systemctl start aiemail-api
curl -fsS http://127.0.0.1:8000/health
systemctl start aiemail-worker aiemail-imap
systemctl is-active aiemail-api aiemail-worker aiemail-imap
```

Do not run `alembic downgrade` during an application rollback unless a database
restore has been explicitly chosen; downgrading `0023 -> 0022` deletes durable
batch history, and `0022 -> 0021` deletes disposition audit and referral data.
