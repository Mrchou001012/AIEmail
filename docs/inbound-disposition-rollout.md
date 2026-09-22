# Inbound disposition rollout template

This public document contains only a vendor-neutral rollout outline. Production
domains, mailboxes, URLs, database commands, service paths, and company-specific
classification examples belong in a private runbook.

Recommended rollout sequence:

1. Configure internal domains and provider credentials privately.
2. Run classification in observation mode with automatic mutation disabled.
3. Review a representative, access-controlled sample of results.
4. Confirm replies, forwards, bounces, automated responses, suppliers, and
   unrelated service offers are handled according to the organization's
   approved policy.
5. Enable reviewer-confirmed actions before enabling any automatic apply path.
6. Monitor audit events, failure queues, and outbound safety gates during the
   staged rollout.
7. Keep a tested rollback that disables classification and action application
   independently.

Never place real message bodies, employee addresses, customer identifiers, or
production administration links in this repository.
