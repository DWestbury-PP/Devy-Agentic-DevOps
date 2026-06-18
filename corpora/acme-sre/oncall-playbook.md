# Acme Payments — On-Call Playbook

> **Fiction** — demo content for Acme Payments.

## Rotation

- **Primary** and **secondary** SRE on a weekly rotation (handover Mondays 10:00
  ET). Schedule lives in PagerDuty service **"Acme Payments — Core"**.
- Primary acknowledges pages within **5 minutes**. If unacknowledged, PagerDuty
  auto-escalates to secondary after 5 minutes, then to the SRE manager after 15.

## Severity definitions

| Sev | Definition | Response |
| --- | --- | --- |
| **SEV-1** | Customer-facing outage or data-integrity risk (checkout down, ledger write failures, DB writer down). | Page immediately; open incident channel; notify manager. |
| **SEV-2** | Significant degradation, SLO breach, no full outage (elevated latency, partial errors). | Page primary; mitigate per runbook. |
| **SEV-3** | Minor/contained, no customer impact. | Handle in business hours. |

## Incident procedure

1. **Acknowledge** the page in PagerDuty.
2. **Open** an incident channel: `#acme-incident` (Slack). Post the alert, time,
   and severity.
3. **Assign roles** for SEV-1: Incident Commander (IC), Ops lead, Comms lead.
   For SEV-2 the primary can hold IC + Ops.
4. **Mitigate** using the relevant runbook (`runbooks/`). Mitigation before
   root-cause — stop the bleeding first.
5. **Communicate** status every 15 min for SEV-1 (every 30 for SEV-2) in the
   incident channel.
6. **Resolve** when the SLO is restored and stable for the runbook's stated
   verification window.
7. **Postmortem** within 3 business days for any SEV-1 or SEV-2 (blameless;
   template in `postmortems/`).

## Escalation contacts

- **Checkout service owner**: team `@acme-checkout` (Slack), for `acme-checkout`
  / `acme-risk` issues.
- **Data team**: `@acme-data`, for Aurora / runaway query / failover decisions.
- **SRE manager**: auto-paged on SEV-1 or after 15 min unacknowledged.

## Golden rules

- Mitigate first, diagnose second.
- Any temporary risk-policy change (e.g. `RISK_FAIL_OPEN=true`) must be logged in
  the incident timeline and reverted before close.
- If you're unsure whether to fail over the database, escalate to `@acme-data` —
  do not fail over with a lagging or absent replica.
