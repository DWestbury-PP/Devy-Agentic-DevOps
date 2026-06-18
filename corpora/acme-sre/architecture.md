# Acme Payments — Platform Architecture

> **Fiction.** Acme Payments is an invented company used to demonstrate
> retrieval. Nothing here describes a real system.

## Overview

Acme Payments runs a card-processing platform on AWS (us-east-1 primary,
us-west-2 warm standby). Traffic enters through CloudFront → ALB → the API
gateway service (`acme-gateway`), which fans out to the core services below.

## Core services

| Service | Language | Datastore | Notes |
| --- | --- | --- | --- |
| `acme-gateway` | Go | — | Edge auth, rate limiting, request routing. |
| `acme-checkout` | Java (Spring) | `payments-pg` (Aurora PostgreSQL) | Owns the checkout/authorization flow. Latency-critical. |
| `acme-ledger` | Java (Spring) | `ledger-pg` (Aurora PostgreSQL) | Double-entry ledger; source of truth for balances. |
| `acme-risk` | Python | Redis + feature store | Real-time fraud scoring; called inline by checkout. |
| `acme-notify` | Node.js | SQS | Email/SMS receipts; fully async, non-critical path. |

## Data stores

- **`payments-pg`** — Aurora PostgreSQL cluster, 1 writer + 2 readers.
  `acme-checkout` writes here. Failover target: the writer endpoint
  `payments-pg.cluster-xxxx.us-east-1.rds.amazonaws.com`.
- **`ledger-pg`** — Aurora PostgreSQL cluster, 1 writer + 1 reader.
- **Redis** — ElastiCache, used by `acme-risk` for feature caching and by
  `acme-gateway` for rate-limit counters.

## Critical path

`acme-gateway → acme-checkout → (acme-risk, payments-pg) → acme-ledger`

A checkout authorization makes a **synchronous** call to `acme-risk`. If
`acme-risk` is slow, checkout latency rises directly. This coupling is the root
cause of the May 2026 incident (see postmortems).

## SLOs

- **Checkout authorization p99 latency**: < 800 ms (alert at 1200 ms for 5 min).
- **Checkout success rate**: > 99.9% over a rolling 30 min window.
- **Ledger write durability**: 100% (any ledger write failure pages immediately).

## Dashboards & alerting

- Grafana: `Acme / Checkout Golden Signals`, `Acme / Aurora Health`.
- Alerts route through PagerDuty service **"Acme Payments — Core"** to the
  on-call SRE. See the on-call playbook for escalation.
