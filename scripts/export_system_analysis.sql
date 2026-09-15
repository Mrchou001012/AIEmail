-- AIEmail production analysis export (PostgreSQL 15 / psql)
--
-- Read-only: this script never changes application data.
-- It intentionally excludes email bodies, raw MIME, attachments, prompts,
-- AI parsed output, and job payloads. Subjects, customer names, and addresses
-- are included because they are needed for customer-level analysis.
--
-- Usage on the production server:
--   sudo install -d -o postgres -g postgres /tmp/aiemail-analysis
--   cd /tmp
--   sudo -u postgres psql -X -d sales_agent \
--     -v output_dir=/tmp/aiemail-analysis \
--     -v from_ts='2026-08-01 00:00:00+08' \
--     -v to_ts='2026-09-16 00:00:00+08' \
--     -v report_timezone=Asia/Shanghai \
--     -f /opt/aiemail/scripts/export_system_analysis.sql
--   sudo tar -C /tmp -czf /tmp/aiemail-analysis.tar.gz aiemail-analysis
--
-- from_ts is inclusive; to_ts is exclusive. If omitted, the window is the
-- latest 30 days. output_dir must exist and should not contain spaces.
-- Internal test traffic involving zhoulei@lanyachem.com is excluded by default.
-- Override only when needed: -v excluded_recipient=another-test@example.com

\set ON_ERROR_STOP on

\if :{?output_dir}
\else
  \echo 'ERROR: output_dir is required (example: -v output_dir=/tmp/aiemail-analysis)'
  \quit 3
\endif

\if :{?from_ts}
\else
  \set from_ts ''
\endif

\if :{?to_ts}
\else
  \set to_ts ''
\endif

\if :{?report_timezone}
\else
  \set report_timezone 'Asia/Shanghai'
\endif

\if :{?excluded_recipient}
\else
  \set excluded_recipient 'zhoulei@lanyachem.com'
\endif

SELECT EXISTS (
    SELECT 1 FROM pg_timezone_names WHERE name = :'report_timezone'
) AS timezone_is_valid
\gset

\if :timezone_is_valid
\else
  \echo 'ERROR: report_timezone is not a valid PostgreSQL timezone'
  \quit 3
\endif

SELECT
    COALESCE(NULLIF(:'from_ts', '')::timestamptz, now() - interval '30 days') AS report_from_ts,
    COALESCE(NULLIF(:'to_ts', '')::timestamptz, now()) AS report_to_ts
\gset

SELECT (:'report_from_ts'::timestamptz < :'report_to_ts'::timestamptz) AS window_is_valid
\gset

\if :window_is_valid
\else
  \echo 'ERROR: from_ts must be earlier than to_ts'
  \quit 3
\endif

\set manifest_file :output_dir '/00_manifest.csv'
\set customer_file :output_dir '/01_customer_summary.csv'
\set inbound_file :output_dir '/02_inbound_processing.csv'
\set campaign_file :output_dir '/03_reactivation_campaigns.csv'
\set recipient_file :output_dir '/04_reactivation_recipients.csv'
\set outbox_file :output_dir '/05_outbox_delivery.csv'
\set handoff_file :output_dir '/06_handoffs_and_agent_runs.csv'
\set ai_file :output_dir '/07_ai_invocations.csv'
\set jobs_file :output_dir '/08_jobs_and_backlog.csv'
\set daily_file :output_dir '/09_daily_metrics.csv'

\pset format csv
\pset footer off
\pset null ''
\pset tuples_only off
\timing off

BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

\o :manifest_file
SELECT
    now() AT TIME ZONE :'report_timezone' AS generated_at_local,
    :'report_timezone' AS report_timezone,
    :'report_from_ts'::timestamptz AT TIME ZONE :'report_timezone' AS from_local_inclusive,
    :'report_to_ts'::timestamptz AT TIME ZONE :'report_timezone' AS to_local_exclusive,
    current_database() AS database_name,
    current_user AS database_user,
    version() AS database_version,
    :'excluded_recipient' AS excluded_internal_test_recipient,
    'No body_text, body_html, raw_message, attachments, prompts, parsed_output, or job payloads exported' AS privacy_scope,
    'A system reply is linked exactly by Outbox business_key containing the inbound email id; AI invocations are reported separately at case level' AS attribution_note;
\o

-- One row per customer, with current state plus activity inside the report window.
\o :customer_file
WITH
p AS (
    SELECT
        :'report_from_ts'::timestamptz AS from_ts,
        :'report_to_ts'::timestamptz AS to_ts,
        lower(:'excluded_recipient') AS excluded_email
),
contact_stats AS (
    SELECT
        customer_id,
        count(*) AS contacts_total,
        count(*) FILTER (WHERE NOT suppressed AND lifecycle_status = 'ACTIVE') AS contacts_active,
        count(*) FILTER (WHERE suppressed) AS contacts_suppressed,
        count(*) FILTER (WHERE lifecycle_status = 'DEPARTED') AS contacts_departed
    FROM contacts CROSS JOIN p
    WHERE lower(trim(email)) <> p.excluded_email
    GROUP BY customer_id
),
case_stats AS (
    SELECT
        ca.customer_id,
        count(*) AS cases_total,
        count(*) FILTER (WHERE ca.status = 'ACTIVE') AS cases_active,
        count(*) FILTER (WHERE ca.status = 'WAITING_HUMAN') AS cases_waiting_human,
        count(*) FILTER (WHERE ca.status = 'HUMAN_TAKEOVER') AS cases_human_takeover,
        max(ca.last_activity_at) AS latest_case_activity_at
    FROM cases ca
    JOIN contacts ct ON ct.id = ca.contact_id
    CROSS JOIN p
    WHERE lower(trim(ct.email)) <> p.excluded_email
      AND NOT EXISTS (
          SELECT 1 FROM outbox test_o
          WHERE test_o.case_id = ca.id
            AND lower(trim(test_o.recipient)) = p.excluded_email
      )
    GROUP BY ca.customer_id
),
email_stats AS (
    SELECT
        e.customer_id,
        count(*) FILTER (
            WHERE e.direction = 'INBOUND' AND NOT e.is_bounce AND NOT e.is_automated_reply
              AND e.received_at >= p.from_ts AND e.received_at < p.to_ts
        ) AS real_inbound_in_window,
        count(*) FILTER (
            WHERE e.direction = 'INBOUND' AND e.is_automated_reply
              AND e.received_at >= p.from_ts AND e.received_at < p.to_ts
        ) AS automated_inbound_in_window,
        count(*) FILTER (
            WHERE e.direction = 'INBOUND' AND e.is_bounce
              AND e.received_at >= p.from_ts AND e.received_at < p.to_ts
        ) AS bounces_in_window,
        max(e.received_at) FILTER (
            WHERE e.direction = 'INBOUND' AND NOT e.is_bounce AND NOT e.is_automated_reply
        ) AS latest_real_inbound_at
    FROM emails e CROSS JOIN p
    WHERE e.customer_id IS NOT NULL
      AND lower(trim(e.from_address)) <> p.excluded_email
      AND NOT EXISTS (
          SELECT 1 FROM contacts test_ct
          WHERE test_ct.id = e.contact_id
            AND lower(trim(test_ct.email)) = p.excluded_email
      )
      AND NOT EXISTS (
          SELECT 1 FROM outbox test_o
          WHERE test_o.case_id = e.case_id
            AND lower(trim(test_o.recipient)) = p.excluded_email
      )
    GROUP BY e.customer_id
),
outbox_stats AS (
    SELECT
        c.customer_id,
        count(*) FILTER (WHERE o.sent_at >= p.from_ts AND o.sent_at < p.to_ts) AS sent_in_window,
        count(*) FILTER (
            WHERE o.sent_at >= p.from_ts AND o.sent_at < p.to_ts
              AND (o.business_key LIKE 'inbound-reply:%'
                   OR o.business_key LIKE 'inbound-product-list:%'
                   OR o.business_key LIKE 'inbound-coa:%'
                   OR o.business_key LIKE 'inbound-coa-followup:%')
        ) AS system_replies_sent_in_window,
        count(*) FILTER (
            WHERE o.created_at >= p.from_ts AND o.created_at < p.to_ts AND o.status = 'FAILED'
        ) AS outbox_failed_in_window,
        max(o.sent_at) AS latest_sent_at
    FROM outbox o
    JOIN cases c ON c.id = o.case_id
    CROSS JOIN p
    WHERE lower(trim(o.recipient)) <> p.excluded_email
    GROUP BY c.customer_id
),
handoff_stats AS (
    SELECT
        c.customer_id,
        count(*) FILTER (WHERE h.created_at >= p.from_ts AND h.created_at < p.to_ts) AS handoffs_in_window,
        count(*) FILTER (WHERE h.status = 'OPEN') AS handoffs_open
    FROM handoffs h
    JOIN cases c ON c.id = h.case_id
    JOIN contacts ct ON ct.id = c.contact_id
    CROSS JOIN p
    WHERE lower(trim(ct.email)) <> p.excluded_email
      AND NOT EXISTS (
          SELECT 1 FROM outbox test_o
          WHERE test_o.case_id = c.id
            AND lower(trim(test_o.recipient)) = p.excluded_email
      )
    GROUP BY c.customer_id
),
ai_stats AS (
    SELECT
        c.customer_id,
        count(*) AS ai_calls_in_window,
        count(*) FILTER (WHERE a.success) AS ai_successes_in_window,
        count(*) FILTER (WHERE NOT a.success) AS ai_failures_in_window,
        coalesce(sum(a.input_tokens), 0) AS ai_input_tokens_in_window,
        coalesce(sum(a.output_tokens), 0) AS ai_output_tokens_in_window
    FROM ai_invocations a
    JOIN cases c ON c.id = a.case_id
    JOIN contacts ct ON ct.id = c.contact_id
    CROSS JOIN p
    WHERE a.created_at >= p.from_ts AND a.created_at < p.to_ts
      AND lower(trim(ct.email)) <> p.excluded_email
      AND NOT EXISTS (
          SELECT 1 FROM outbox test_o
          WHERE test_o.case_id = c.id
            AND lower(trim(test_o.recipient)) = p.excluded_email
      )
    GROUP BY c.customer_id
),
reactivation_stats AS (
    SELECT
        rr.customer_id,
        count(*) FILTER (WHERE rr.sent_at >= p.from_ts AND rr.sent_at < p.to_ts) AS reactivation_sent_in_window,
        count(*) FILTER (WHERE rr.replied_at >= p.from_ts AND rr.replied_at < p.to_ts) AS reactivation_replied_in_window
    FROM reactivation_recipients rr
    JOIN contacts ct ON ct.id = rr.contact_id
    CROSS JOIN p
    WHERE lower(trim(ct.email)) <> p.excluded_email
    GROUP BY rr.customer_id
)
SELECT
    cu.id AS customer_id,
    cu.company_name,
    cu.language,
    cu.auto_send_allowed,
    cu.do_not_contact,
    cu.qualification_status,
    cu.qualification_reason,
    cu.qualified_at AT TIME ZONE :'report_timezone' AS qualified_at_local,
    coalesce(ct.contacts_total, 0) AS contacts_total,
    coalesce(ct.contacts_active, 0) AS contacts_active,
    coalesce(ct.contacts_suppressed, 0) AS contacts_suppressed,
    coalesce(ct.contacts_departed, 0) AS contacts_departed,
    coalesce(cs.cases_total, 0) AS cases_total,
    coalesce(cs.cases_active, 0) AS cases_active,
    coalesce(cs.cases_waiting_human, 0) AS cases_waiting_human,
    coalesce(cs.cases_human_takeover, 0) AS cases_human_takeover,
    coalesce(es.real_inbound_in_window, 0) AS real_inbound_in_window,
    coalesce(es.automated_inbound_in_window, 0) AS automated_inbound_in_window,
    coalesce(es.bounces_in_window, 0) AS bounces_in_window,
    coalesce(os.sent_in_window, 0) AS sent_in_window,
    coalesce(os.system_replies_sent_in_window, 0) AS system_replies_sent_in_window,
    coalesce(os.outbox_failed_in_window, 0) AS outbox_failed_in_window,
    coalesce(hs.handoffs_in_window, 0) AS handoffs_in_window,
    coalesce(hs.handoffs_open, 0) AS handoffs_open,
    coalesce(ai.ai_calls_in_window, 0) AS ai_calls_in_window,
    coalesce(ai.ai_successes_in_window, 0) AS ai_successes_in_window,
    coalesce(ai.ai_failures_in_window, 0) AS ai_failures_in_window,
    coalesce(ai.ai_input_tokens_in_window, 0) AS ai_input_tokens_in_window,
    coalesce(ai.ai_output_tokens_in_window, 0) AS ai_output_tokens_in_window,
    coalesce(rs.reactivation_sent_in_window, 0) AS reactivation_sent_in_window,
    coalesce(rs.reactivation_replied_in_window, 0) AS reactivation_replied_in_window,
    es.latest_real_inbound_at AT TIME ZONE :'report_timezone' AS latest_real_inbound_at_local,
    os.latest_sent_at AT TIME ZONE :'report_timezone' AS latest_sent_at_local,
    cs.latest_case_activity_at AT TIME ZONE :'report_timezone' AS latest_case_activity_at_local,
    cu.created_at AT TIME ZONE :'report_timezone' AS customer_created_at_local
FROM customers cu
LEFT JOIN contact_stats ct ON ct.customer_id = cu.id
LEFT JOIN case_stats cs ON cs.customer_id = cu.id
LEFT JOIN email_stats es ON es.customer_id = cu.id
LEFT JOIN outbox_stats os ON os.customer_id = cu.id
LEFT JOIN handoff_stats hs ON hs.customer_id = cu.id
LEFT JOIN ai_stats ai ON ai.customer_id = cu.id
LEFT JOIN reactivation_stats rs ON rs.customer_id = cu.id
WHERE EXISTS (
    SELECT 1
    FROM contacts visible_ct
    WHERE visible_ct.customer_id = cu.id
      AND lower(trim(visible_ct.email)) <> lower(:'excluded_recipient')
)
ORDER BY cu.id;
\o

-- One row per inbound message. system_reply_* is an exact business-key match.
\o :inbound_file
WITH p AS (
    SELECT
        :'report_from_ts'::timestamptz AS from_ts,
        :'report_to_ts'::timestamptz AS to_ts,
        lower(:'excluded_recipient') AS excluded_email
)
SELECT
    e.id AS inbound_email_id,
    e.received_at AT TIME ZONE :'report_timezone' AS received_at_local,
    e.is_history,
    e.customer_id,
    cu.company_name,
    e.contact_id,
    ct.name AS contact_name,
    e.from_address,
    left(e.subject, 500) AS subject,
    e.case_id,
    ca.stage AS case_stage,
    ca.status AS case_status,
    e.is_bounce,
    e.bounce_type,
    e.is_automated_reply,
    e.automated_reply_type,
    e.disposition_type,
    e.disposition_confidence,
    e.disposition_handled_at AT TIME ZONE :'report_timezone' AS disposition_handled_at_local,
    h.id AS handoff_id,
    h.reason_code AS handoff_reason,
    h.status AS handoff_status,
    ar.id AS agent_run_id,
    ar.status AS agent_run_status,
    ar.current_step AS agent_current_step,
    reply.id AS system_reply_outbox_id,
    reply.message_kind AS system_reply_kind,
    reply.status AS system_reply_status,
    reply.attempts AS system_reply_attempts,
    reply.created_at AT TIME ZONE :'report_timezone' AS system_reply_created_at_local,
    reply.sent_at AT TIME ZONE :'report_timezone' AS system_reply_sent_at_local,
    CASE
        WHEN e.is_bounce THEN 'BOUNCE_HANDLED'
        WHEN e.is_automated_reply THEN 'AUTOMATED_REPLY_HANDLED'
        WHEN reply.status = 'SENT' THEN 'SYSTEM_REPLY_SENT'
        WHEN reply.id IS NOT NULL THEN 'SYSTEM_REPLY_' || reply.status
        WHEN h.id IS NOT NULL THEN 'HUMAN_HANDOFF_' || h.status
        WHEN e.disposition_handled_at IS NOT NULL THEN 'DISPOSITION_HANDLED_NO_REPLY'
        ELSE 'NO_RECORDED_OUTCOME'
    END AS processing_outcome,
    CASE WHEN reply.sent_at IS NOT NULL
        THEN round(extract(epoch FROM (reply.sent_at - e.received_at)) / 60.0, 2)
    END AS response_minutes,
    left(reply.last_error, 1000) AS system_reply_last_error
FROM emails e
CROSS JOIN p
LEFT JOIN customers cu ON cu.id = e.customer_id
LEFT JOIN contacts ct ON ct.id = e.contact_id
LEFT JOIN cases ca ON ca.id = e.case_id
LEFT JOIN handoffs h ON h.source_email_id = e.id
LEFT JOIN agent_runs ar ON ar.source_email_id = e.id
LEFT JOIN LATERAL (
    SELECT o.*
    FROM outbox o
    WHERE o.business_key = 'inbound-reply:' || e.id
       OR o.business_key LIKE 'inbound-reply:' || e.id || ':%'
       OR o.business_key = 'inbound-product-list:' || e.id
       OR o.business_key LIKE 'inbound-product-list:' || e.id || ':%'
       OR o.business_key = 'inbound-coa:' || e.id
       OR o.business_key LIKE 'inbound-coa:' || e.id || ':%'
       OR o.business_key = 'inbound-coa-followup:' || e.id
       OR o.business_key LIKE 'inbound-coa-followup:' || e.id || ':%'
    ORDER BY o.created_at DESC, o.id DESC
    LIMIT 1
) reply ON true
WHERE e.direction = 'INBOUND'
  AND e.received_at >= p.from_ts
  AND e.received_at < p.to_ts
  AND lower(trim(e.from_address)) <> p.excluded_email
  AND coalesce(lower(trim(ct.email)), '') <> p.excluded_email
  AND NOT EXISTS (
      SELECT 1 FROM outbox test_o
      WHERE test_o.case_id = e.case_id
        AND lower(trim(test_o.recipient)) = p.excluded_email
  )
ORDER BY e.received_at, e.id;
\o

-- Campaign-level selection, delivery, failure, and reply rates.
\o :campaign_file
WITH visible_recipients AS (
    SELECT rr.*
    FROM reactivation_recipients rr
    JOIN contacts ct ON ct.id = rr.contact_id
    WHERE lower(trim(ct.email)) <> lower(:'excluded_recipient')
)
SELECT
    rc.id AS campaign_id,
    rc.name,
    rc.status,
    rc.reply_filter,
    rc.min_inactive_days,
    rc.daily_limit,
    rc.timezone,
    rc.start_date,
    rc.started_at AT TIME ZONE :'report_timezone' AS started_at_local,
    rc.paused_at AT TIME ZONE :'report_timezone' AS paused_at_local,
    rc.completed_at AT TIME ZONE :'report_timezone' AS completed_at_local,
    count(rr.id) AS recipients_total,
    count(*) FILTER (WHERE rr.eligible) AS eligible,
    count(*) FILTER (WHERE rr.selected) AS selected,
    count(*) FILTER (WHERE rr.status = 'SENT') AS sent_status,
    count(*) FILTER (WHERE rr.sent_at IS NOT NULL) AS delivered,
    count(*) FILTER (WHERE rr.status = 'REPLIED' OR rr.replied_at IS NOT NULL) AS replied,
    count(*) FILTER (WHERE rr.status = 'FAILED') AS failed,
    count(*) FILTER (WHERE rr.status = 'SKIPPED') AS skipped,
    count(*) FILTER (WHERE rr.status = 'EXCLUDED') AS excluded,
    count(*) FILTER (WHERE rr.status IN ('SCHEDULED', 'QUEUED')) AS still_queued,
    round(
        100.0 * count(*) FILTER (WHERE rr.status = 'REPLIED' OR rr.replied_at IS NOT NULL)
        / nullif(count(*) FILTER (WHERE rr.sent_at IS NOT NULL), 0),
        2
    ) AS reply_rate_pct,
    min(rr.sent_at) AT TIME ZONE :'report_timezone' AS first_sent_at_local,
    max(rr.sent_at) AT TIME ZONE :'report_timezone' AS last_sent_at_local,
    max(rr.replied_at) AT TIME ZONE :'report_timezone' AS last_reply_at_local
FROM reactivation_campaigns rc
LEFT JOIN visible_recipients rr ON rr.campaign_id = rc.id
GROUP BY rc.id
HAVING count(rr.id) > 0
    OR NOT EXISTS (
        SELECT 1 FROM reactivation_recipients any_rr
        WHERE any_rr.campaign_id = rc.id
    )
ORDER BY rc.id;
\o

-- Recipient-level campaign result. The reply email is the closest real inbound
-- message for that contact around replied_at; this is explicitly a heuristic.
\o :recipient_file
WITH p AS (
    SELECT
        :'report_from_ts'::timestamptz AS from_ts,
        :'report_to_ts'::timestamptz AS to_ts,
        lower(:'excluded_recipient') AS excluded_email
)
SELECT
    rr.id AS recipient_id,
    rr.campaign_id,
    rc.name AS campaign_name,
    rr.customer_id,
    cu.company_name,
    rr.contact_id,
    ct.name AS contact_name,
    ct.email,
    ct.suppressed AS contact_suppressed,
    ct.lifecycle_status,
    eas.preflight_status,
    eas.suppressed AS address_suppressed,
    eas.suppression_reason,
    eas.last_bounce_type,
    rr.eligible,
    rr.selected,
    rr.status AS recipient_status,
    rr.exclusion_reason,
    rr.previous_reactivation_count,
    rr.scheduled_for AT TIME ZONE :'report_timezone' AS scheduled_for_local,
    rr.sent_at AT TIME ZONE :'report_timezone' AS sent_at_local,
    rr.replied_at AT TIME ZONE :'report_timezone' AS replied_at_local,
    rr.outbox_id,
    campaign_outbox.status AS campaign_outbox_status,
    campaign_outbox.attempts AS campaign_outbox_attempts,
    left(campaign_outbox.last_error, 1000) AS campaign_outbox_last_error,
    reply_email.id AS reply_email_id_heuristic,
    left(reply_email.subject, 500) AS reply_subject,
    reply_outbox.id AS reply_outbox_id,
    reply_outbox.message_kind AS reply_outbox_kind,
    reply_outbox.status AS reply_outbox_status,
    reply_outbox.sent_at AT TIME ZONE :'report_timezone' AS system_reply_sent_at_local,
    CASE
        WHEN rr.replied_at IS NULL THEN 'NO_CUSTOMER_REPLY'
        WHEN reply_outbox.status = 'SENT' THEN 'CUSTOMER_REPLIED_SYSTEM_REPLIED'
        WHEN reply_outbox.id IS NOT NULL THEN 'CUSTOMER_REPLIED_REPLY_' || reply_outbox.status
        WHEN reply_handoff.id IS NOT NULL THEN 'CUSTOMER_REPLIED_HUMAN_HANDOFF_' || reply_handoff.status
        ELSE 'CUSTOMER_REPLIED_NO_RECORDED_REPLY'
    END AS reply_processing_outcome
FROM reactivation_recipients rr
JOIN reactivation_campaigns rc ON rc.id = rr.campaign_id
JOIN customers cu ON cu.id = rr.customer_id
JOIN contacts ct ON ct.id = rr.contact_id
LEFT JOIN email_address_statuses eas ON eas.email = lower(trim(ct.email))
LEFT JOIN outbox campaign_outbox ON campaign_outbox.id = rr.outbox_id
LEFT JOIN LATERAL (
    SELECT e.*
    FROM emails e
    WHERE rr.replied_at IS NOT NULL
      AND e.contact_id = rr.contact_id
      AND e.direction = 'INBOUND'
      AND NOT e.is_bounce
      AND NOT e.is_automated_reply
      AND e.received_at >= rr.sent_at
      AND e.received_at <= rr.replied_at + interval '1 day'
    ORDER BY abs(extract(epoch FROM (e.received_at - rr.replied_at))), e.id
    LIMIT 1
) reply_email ON true
LEFT JOIN handoffs reply_handoff ON reply_handoff.source_email_id = reply_email.id
LEFT JOIN LATERAL (
    SELECT o.*
    FROM outbox o
    WHERE o.business_key = 'inbound-reply:' || reply_email.id
       OR o.business_key LIKE 'inbound-reply:' || reply_email.id || ':%'
       OR o.business_key = 'inbound-product-list:' || reply_email.id
       OR o.business_key LIKE 'inbound-product-list:' || reply_email.id || ':%'
       OR o.business_key = 'inbound-coa:' || reply_email.id
       OR o.business_key LIKE 'inbound-coa:' || reply_email.id || ':%'
       OR o.business_key = 'inbound-coa-followup:' || reply_email.id
       OR o.business_key LIKE 'inbound-coa-followup:' || reply_email.id || ':%'
    ORDER BY o.created_at DESC, o.id DESC
    LIMIT 1
) reply_outbox ON true
CROSS JOIN p
WHERE rc.created_at < p.to_ts
  AND coalesce(rc.completed_at, p.to_ts) >= p.from_ts
  AND lower(trim(ct.email)) <> p.excluded_email
ORDER BY rr.campaign_id, rr.id;
\o

-- Every Outbox item created during the window, excluding raw MIME/body.
\o :outbox_file
WITH p AS (
    SELECT
        :'report_from_ts'::timestamptz AS from_ts,
        :'report_to_ts'::timestamptz AS to_ts,
        lower(:'excluded_recipient') AS excluded_email
)
SELECT
    o.id AS outbox_id,
    o.case_id,
    ca.customer_id,
    cu.company_name,
    ca.contact_id,
    ct.name AS contact_name,
    o.recipient,
    outbound.subject,
    o.message_kind,
    o.business_key,
    o.status,
    o.attempts,
    o.sent_via,
    o.created_at AT TIME ZONE :'report_timezone' AS created_at_local,
    o.available_at AT TIME ZONE :'report_timezone' AS available_at_local,
    o.sent_at AT TIME ZONE :'report_timezone' AS sent_at_local,
    CASE WHEN o.sent_at IS NOT NULL
        THEN round(extract(epoch FROM (o.sent_at - o.created_at)), 2)
    END AS delivery_seconds,
    o.approval_handoff_id,
    o.human_approved_by,
    o.human_approved_at AT TIME ZONE :'report_timezone' AS human_approved_at_local,
    coalesce(outbound.attachment_count, 0) AS attachment_count,
    left(o.last_error, 1000) AS last_error
FROM outbox o
CROSS JOIN p
LEFT JOIN cases ca ON ca.id = o.case_id
LEFT JOIN customers cu ON cu.id = ca.customer_id
LEFT JOIN contacts ct ON ct.id = ca.contact_id
LEFT JOIN LATERAL (
    SELECT
        e.subject,
        json_array_length(coalesce(e.attachment_metadata, '[]'::json)) AS attachment_count
    FROM emails e
    WHERE e.direction = 'OUTBOUND' AND e.message_id = o.message_id
    ORDER BY e.id DESC
    LIMIT 1
) outbound ON true
WHERE o.created_at >= p.from_ts AND o.created_at < p.to_ts
  AND lower(trim(o.recipient)) <> p.excluded_email
ORDER BY o.created_at, o.id;
\o

-- Human handoffs, durable agent runs, and assistance request progress.
\o :handoff_file
WITH p AS (
    SELECT
        :'report_from_ts'::timestamptz AS from_ts,
        :'report_to_ts'::timestamptz AS to_ts,
        lower(:'excluded_recipient') AS excluded_email
), assistance AS (
    SELECT
        handoff_id,
        count(*) AS requests_total,
        count(*) FILTER (WHERE status = 'OPEN') AS requests_open,
        count(*) FILTER (WHERE status = 'ANSWERED') AS requests_answered,
        count(*) FILTER (WHERE status = 'APPLIED') AS requests_applied,
        string_agg(request_type || ':' || status, '; ' ORDER BY id) AS request_states,
        max(answered_at) AS latest_answered_at,
        max(applied_at) AS latest_applied_at
    FROM assistance_requests
    GROUP BY handoff_id
)
SELECT
    h.id AS handoff_id,
    h.case_id,
    ca.customer_id,
    cu.company_name,
    ca.contact_id,
    ct.name AS contact_name,
    ct.email,
    h.source_email_id,
    h.reason_code,
    h.status AS handoff_status,
    h.dingtalk_status,
    left(h.summary, 1000) AS summary,
    left(h.resolution_note, 1000) AS resolution_note,
    h.created_at AT TIME ZONE :'report_timezone' AS handoff_created_at_local,
    h.updated_at AT TIME ZONE :'report_timezone' AS handoff_updated_at_local,
    ar.id AS agent_run_id,
    ar.run_kind,
    ar.status AS agent_run_status,
    ar.current_step,
    ar.version AS agent_run_version,
    left(ar.last_error, 1000) AS agent_last_error,
    ar.completed_at AT TIME ZONE :'report_timezone' AS agent_completed_at_local,
    coalesce(a.requests_total, 0) AS assistance_requests_total,
    coalesce(a.requests_open, 0) AS assistance_requests_open,
    coalesce(a.requests_answered, 0) AS assistance_requests_answered,
    coalesce(a.requests_applied, 0) AS assistance_requests_applied,
    a.request_states,
    a.latest_answered_at AT TIME ZONE :'report_timezone' AS latest_answered_at_local,
    a.latest_applied_at AT TIME ZONE :'report_timezone' AS latest_applied_at_local,
    h.extracted_facts ->> 'intent' AS detected_intent,
    h.extracted_facts ->> 'intent_confidence' AS intent_confidence,
    h.extracted_facts ->> 'requested_product_name' AS requested_product_name,
    h.extracted_facts ->> 'product_confidence' AS product_confidence
FROM handoffs h
CROSS JOIN p
LEFT JOIN cases ca ON ca.id = h.case_id
LEFT JOIN customers cu ON cu.id = ca.customer_id
LEFT JOIN contacts ct ON ct.id = ca.contact_id
LEFT JOIN agent_runs ar ON ar.handoff_id = h.id
LEFT JOIN assistance a ON a.handoff_id = h.id
WHERE (
       (h.created_at >= p.from_ts AND h.created_at < p.to_ts)
    OR (h.updated_at >= p.from_ts AND h.updated_at < p.to_ts)
    OR h.status = 'OPEN'
  )
  AND coalesce(lower(trim(ct.email)), '') <> p.excluded_email
  AND NOT EXISTS (
      SELECT 1 FROM outbox test_o
      WHERE test_o.case_id = h.case_id
        AND lower(trim(test_o.recipient)) = p.excluded_email
  )
ORDER BY h.created_at, h.id;
\o

-- AI usage and success are case-level facts; no prompt or parsed output is exported.
\o :ai_file
WITH p AS (
    SELECT
        :'report_from_ts'::timestamptz AS from_ts,
        :'report_to_ts'::timestamptz AS to_ts,
        lower(:'excluded_recipient') AS excluded_email
)
SELECT
    a.id AS ai_invocation_id,
    a.created_at AT TIME ZONE :'report_timezone' AS created_at_local,
    a.case_id,
    ca.customer_id,
    cu.company_name,
    ca.contact_id,
    ct.email,
    a.provider,
    a.model,
    a.purpose,
    a.success,
    a.error_type,
    a.input_tokens,
    a.output_tokens,
    a.request_hash
FROM ai_invocations a
CROSS JOIN p
LEFT JOIN cases ca ON ca.id = a.case_id
LEFT JOIN customers cu ON cu.id = ca.customer_id
LEFT JOIN contacts ct ON ct.id = ca.contact_id
WHERE a.created_at >= p.from_ts AND a.created_at < p.to_ts
  AND coalesce(lower(trim(ct.email)), '') <> p.excluded_email
  AND NOT EXISTS (
      SELECT 1 FROM outbox test_o
      WHERE test_o.case_id = a.case_id
        AND lower(trim(test_o.recipient)) = p.excluded_email
  )
ORDER BY a.created_at, a.id;
\o

-- Jobs created in the window plus any currently pending/running/failed job.
\o :jobs_file
WITH p AS (
    SELECT
        :'report_from_ts'::timestamptz AS from_ts,
        :'report_to_ts'::timestamptz AS to_ts,
        lower(:'excluded_recipient') AS excluded_email
)
SELECT
    j.id AS job_id,
    j.kind,
    j.status,
    j.attempts,
    j.max_attempts,
    j.created_at AT TIME ZONE :'report_timezone' AS created_at_local,
    j.updated_at AT TIME ZONE :'report_timezone' AS updated_at_local,
    j.available_at AT TIME ZONE :'report_timezone' AS available_at_local,
    j.locked_at AT TIME ZONE :'report_timezone' AS locked_at_local,
    j.locked_by,
    round(extract(epoch FROM (now() - j.created_at)) / 60.0, 2) AS age_minutes,
    j.payload ->> 'campaign_id' AS campaign_id,
    j.payload ->> 'recipient_id' AS recipient_id,
    j.payload ->> 'email_id' AS email_id,
    j.payload ->> 'handoff_id' AS handoff_id,
    coalesce(j.payload ->> 'run_id', j.payload ->> 'agent_run_id') AS agent_run_id,
    j.payload ->> 'outbox_id' AS outbox_id,
    left(j.last_error, 1000) AS last_error
FROM jobs j CROSS JOIN p
WHERE (
       (j.created_at >= p.from_ts AND j.created_at < p.to_ts)
    OR j.status IN ('PENDING', 'RUNNING', 'FAILED')
  )
  AND coalesce(lower(trim(j.payload ->> 'recipient')), '') <> p.excluded_email
  AND NOT EXISTS (
      SELECT 1 FROM outbox test_o
      WHERE test_o.id::text = j.payload ->> 'outbox_id'
        AND lower(trim(test_o.recipient)) = p.excluded_email
  )
  AND NOT EXISTS (
      SELECT 1
      FROM reactivation_recipients test_rr
      JOIN contacts test_ct ON test_ct.id = test_rr.contact_id
      WHERE test_rr.id::text = j.payload ->> 'recipient_id'
        AND lower(trim(test_ct.email)) = p.excluded_email
  )
  AND NOT EXISTS (
      SELECT 1 FROM emails test_e
      LEFT JOIN contacts test_ct ON test_ct.id = test_e.contact_id
      WHERE test_e.id::text = j.payload ->> 'email_id'
        AND (
            lower(trim(test_e.from_address)) = p.excluded_email
            OR lower(trim(test_ct.email)) = p.excluded_email
            OR EXISTS (
                SELECT 1 FROM outbox test_o
                WHERE test_o.case_id = test_e.case_id
                  AND lower(trim(test_o.recipient)) = p.excluded_email
            )
        )
  )
  AND NOT EXISTS (
      SELECT 1
      FROM handoffs test_h
      LEFT JOIN cases test_ca ON test_ca.id = test_h.case_id
      LEFT JOIN contacts test_ct ON test_ct.id = test_ca.contact_id
      WHERE test_h.id::text = j.payload ->> 'handoff_id'
        AND (
            lower(trim(test_ct.email)) = p.excluded_email
            OR EXISTS (
                SELECT 1 FROM outbox test_o
                WHERE test_o.case_id = test_h.case_id
                  AND lower(trim(test_o.recipient)) = p.excluded_email
            )
        )
  )
  AND NOT EXISTS (
      SELECT 1
      FROM agent_runs test_ar
      JOIN cases test_ca ON test_ca.id = test_ar.case_id
      JOIN contacts test_ct ON test_ct.id = test_ca.contact_id
      WHERE test_ar.id::text = coalesce(
          j.payload ->> 'run_id',
          j.payload ->> 'agent_run_id'
      )
        AND (
            lower(trim(test_ct.email)) = p.excluded_email
            OR EXISTS (
                SELECT 1 FROM outbox test_o
                WHERE test_o.case_id = test_ar.case_id
                  AND lower(trim(test_o.recipient)) = p.excluded_email
            )
        )
  )
ORDER BY
    CASE j.status WHEN 'RUNNING' THEN 1 WHEN 'PENDING' THEN 2 WHEN 'FAILED' THEN 3 ELSE 4 END,
    j.created_at,
    j.id;
\o

-- Daily operational totals in report_timezone.
\o :daily_file
WITH
p AS (
    SELECT
        :'report_from_ts'::timestamptz AS from_ts,
        :'report_to_ts'::timestamptz AS to_ts,
        :'report_timezone'::text AS tz,
        lower(:'excluded_recipient') AS excluded_email
),
days AS (
    SELECT generate_series(
        (p.from_ts AT TIME ZONE p.tz)::date,
        ((p.to_ts - interval '1 microsecond') AT TIME ZONE p.tz)::date,
        interval '1 day'
    )::date AS day
    FROM p
),
email_daily AS (
    SELECT
        (e.received_at AT TIME ZONE p.tz)::date AS day,
        count(*) FILTER (WHERE e.direction = 'INBOUND' AND NOT e.is_bounce AND NOT e.is_automated_reply) AS real_inbound,
        count(*) FILTER (WHERE e.direction = 'INBOUND' AND e.is_automated_reply) AS automated_inbound,
        count(*) FILTER (WHERE e.direction = 'INBOUND' AND e.is_bounce) AS bounces
    FROM emails e CROSS JOIN p
    WHERE e.received_at >= p.from_ts AND e.received_at < p.to_ts
      AND lower(trim(e.from_address)) <> p.excluded_email
      AND NOT EXISTS (
          SELECT 1 FROM contacts test_ct
          WHERE test_ct.id = e.contact_id
            AND lower(trim(test_ct.email)) = p.excluded_email
      )
      AND NOT EXISTS (
          SELECT 1 FROM outbox test_o
          WHERE test_o.case_id = e.case_id
            AND lower(trim(test_o.recipient)) = p.excluded_email
      )
    GROUP BY 1
),
outbox_created AS (
    SELECT
        (o.created_at AT TIME ZONE p.tz)::date AS day,
        count(*) AS outbox_created,
        count(*) FILTER (WHERE o.status = 'FAILED') AS outbox_failed,
        count(*) FILTER (WHERE o.status = 'CANCELLED') AS outbox_cancelled
    FROM outbox o CROSS JOIN p
    WHERE o.created_at >= p.from_ts AND o.created_at < p.to_ts
      AND lower(trim(o.recipient)) <> p.excluded_email
    GROUP BY 1
),
outbox_sent AS (
    SELECT (o.sent_at AT TIME ZONE p.tz)::date AS day, count(*) AS outbox_sent
    FROM outbox o CROSS JOIN p
    WHERE o.sent_at >= p.from_ts AND o.sent_at < p.to_ts
      AND lower(trim(o.recipient)) <> p.excluded_email
    GROUP BY 1
),
ai_daily AS (
    SELECT
        (a.created_at AT TIME ZONE p.tz)::date AS day,
        count(*) AS ai_calls,
        count(*) FILTER (WHERE a.success) AS ai_successes,
        count(*) FILTER (WHERE NOT a.success) AS ai_failures,
        coalesce(sum(a.input_tokens), 0) AS input_tokens,
        coalesce(sum(a.output_tokens), 0) AS output_tokens
    FROM ai_invocations a
    LEFT JOIN cases ca ON ca.id = a.case_id
    LEFT JOIN contacts ct ON ct.id = ca.contact_id
    CROSS JOIN p
    WHERE a.created_at >= p.from_ts AND a.created_at < p.to_ts
      AND coalesce(lower(trim(ct.email)), '') <> p.excluded_email
      AND NOT EXISTS (
          SELECT 1 FROM outbox test_o
          WHERE test_o.case_id = a.case_id
            AND lower(trim(test_o.recipient)) = p.excluded_email
      )
    GROUP BY 1
),
handoff_daily AS (
    SELECT (h.created_at AT TIME ZONE p.tz)::date AS day, count(*) AS handoffs_created
    FROM handoffs h
    LEFT JOIN cases ca ON ca.id = h.case_id
    LEFT JOIN contacts ct ON ct.id = ca.contact_id
    CROSS JOIN p
    WHERE h.created_at >= p.from_ts AND h.created_at < p.to_ts
      AND coalesce(lower(trim(ct.email)), '') <> p.excluded_email
      AND NOT EXISTS (
          SELECT 1 FROM outbox test_o
          WHERE test_o.case_id = h.case_id
            AND lower(trim(test_o.recipient)) = p.excluded_email
      )
    GROUP BY 1
),
reactivation_sent AS (
    SELECT (r.sent_at AT TIME ZONE p.tz)::date AS day, count(*) AS reactivation_sent
    FROM reactivation_recipients r
    JOIN contacts ct ON ct.id = r.contact_id
    CROSS JOIN p
    WHERE r.sent_at >= p.from_ts AND r.sent_at < p.to_ts
      AND lower(trim(ct.email)) <> p.excluded_email
    GROUP BY 1
),
reactivation_replied AS (
    SELECT (r.replied_at AT TIME ZONE p.tz)::date AS day, count(*) AS reactivation_replied
    FROM reactivation_recipients r
    JOIN contacts ct ON ct.id = r.contact_id
    CROSS JOIN p
    WHERE r.replied_at >= p.from_ts AND r.replied_at < p.to_ts
      AND lower(trim(ct.email)) <> p.excluded_email
    GROUP BY 1
)
SELECT
    d.day,
    coalesce(e.real_inbound, 0) AS real_inbound,
    coalesce(e.automated_inbound, 0) AS automated_inbound,
    coalesce(e.bounces, 0) AS bounces,
    coalesce(oc.outbox_created, 0) AS outbox_created,
    coalesce(os.outbox_sent, 0) AS outbox_sent,
    coalesce(oc.outbox_failed, 0) AS outbox_failed,
    coalesce(oc.outbox_cancelled, 0) AS outbox_cancelled,
    coalesce(a.ai_calls, 0) AS ai_calls,
    coalesce(a.ai_successes, 0) AS ai_successes,
    coalesce(a.ai_failures, 0) AS ai_failures,
    coalesce(a.input_tokens, 0) AS ai_input_tokens,
    coalesce(a.output_tokens, 0) AS ai_output_tokens,
    coalesce(h.handoffs_created, 0) AS handoffs_created,
    coalesce(rs.reactivation_sent, 0) AS reactivation_sent,
    coalesce(rr.reactivation_replied, 0) AS reactivation_replied
FROM days d
LEFT JOIN email_daily e ON e.day = d.day
LEFT JOIN outbox_created oc ON oc.day = d.day
LEFT JOIN outbox_sent os ON os.day = d.day
LEFT JOIN ai_daily a ON a.day = d.day
LEFT JOIN handoff_daily h ON h.day = d.day
LEFT JOIN reactivation_sent rs ON rs.day = d.day
LEFT JOIN reactivation_replied rr ON rr.day = d.day
ORDER BY d.day;
\o

COMMIT;

\pset format aligned
\echo 'Export completed:' :output_dir
\echo '  00_manifest.csv'
\echo '  01_customer_summary.csv'
\echo '  02_inbound_processing.csv'
\echo '  03_reactivation_campaigns.csv'
\echo '  04_reactivation_recipients.csv'
\echo '  05_outbox_delivery.csv'
\echo '  06_handoffs_and_agent_runs.csv'
\echo '  07_ai_invocations.csv'
\echo '  08_jobs_and_backlog.csv'
\echo '  09_daily_metrics.csv'
