# Demo corpora

Two seed knowledge bases for the retrieval demo. The pipeline is
content-agnostic — point `agentic-devops ingest` at any directory — but these
two show the value from both angles.

| Corpus | What it is | Why it's a good demo |
| --- | --- | --- |
| **repo** (dogfood) | This repository's own docs | Real, free, self-updating, and **provably outside the model's training** — the agent answers questions about *itself*. |
| **acme-sre** | A *fictional* company's SRE knowledge base (`corpora/acme-sre/`) | Shows the target persona: runbooks, on-call playbook, architecture, postmortems. All content is invented. |
| **platform** | The crash-loop runbook (`corpora/platform/`) | The KB half of the RCA demo — pairs with `docker compose --profile demo` so Devy can investigate a live crash-looping container against a real runbook. |

> RAG only impresses when the corpus holds knowledge the base model *can't*
> already answer. Both corpora are designed to be out-of-distribution: your
> project decisions, and Acme's fictional incidents.

## Ingest them

```bash
# Dogfood: index this repo's docs (default seed)
agentic-devops ingest --corpus repo .

# The fictional SRE knowledge base
agentic-devops ingest corpora/acme-sre

# The RCA-demo crash-loop runbook (pairs with `docker compose --profile demo`)
agentic-devops ingest corpora/platform
```

Then ask `ask` things like:

- *repo*: "What safety profiles does the host MCP support, and why was the MCP-Hub idea dropped?"
- *acme-sre*: "What's the runbook for the checkout latency alert?" / "Summarize the last database failover postmortem."

Re-running `ingest` is idempotent — unchanged files are skipped, changed files
re-embedded.

> **acme-sre is fiction.** Acme Payments, its services, hostnames, dashboards,
> and incidents do not exist. It exists only to demonstrate retrieval.
