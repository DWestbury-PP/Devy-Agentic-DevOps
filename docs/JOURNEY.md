# The Journey: Research vs. Reality

> **Background / optional reading.** This is the design lineage, not a guide to
> using Devy — for that, start at the [README](../README.md) and
> [Architecture](architecture.md). It's preserved for anyone curious how the
> design got here (and why the early "MCP Hub" multi-agent concept was abandoned).
>
> This document is deliberately preserved from the original incarnation of this
> repository. Almost everything else here has been deleted and is being rebuilt
> from scratch — but the *thinking* was worth keeping. When I started, I had a
> confident, elaborate mental model of how to apply AI agents to DevOps and SRE
> work. After actually building and operating a real system for a demanding
> production environment, what I landed on was far simpler, and far more useful,
> than what I originally drew up.
>
> I'm keeping this record because the gap between the two is the most valuable
> thing I learned, and because anyone setting out on a similar path is likely to
> start where I started. If this saves you a few months of building the wrong
> thing, it has done its job.

---

## The mission (this part never changed)

The goal was always the same, and still is:

**Use LLMs to remove the toil from DevOps and SRE work — incident response,
diagnostics, observability triage, routine operational questions — while making
the *outputs* more consistent and higher quality than what tired humans produce
under pressure.**

That abstract held up perfectly. Everything *underneath* it changed.

---

## Part 1 — Where my head was at the start (the research)

My early planning began more than a year before this rewrite, when LLMs were
capable but the practices for building agent harnesses were still very new.
Nobody had strong, battle-tested conventions yet, so I reasoned from first
principles — and from first principles, the "obvious" design was a **society of
specialized agents coordinated by a central brain.**

Concretely, the original design proposed:

- **Six role-scoped autonomous agents**, each owning a slice of the domain:
  - Infrastructure Health Agent
  - CI/CD & Deployment Agent
  - Application Performance Agent
  - Observability & Monitoring Agent
  - External Connectivity Agent
  - Security & Compliance Agent
- Each agent implemented as its **own MCP server**, with its own system context,
  its own narrow toolset, and the ability to run continuously and surface
  anomalies on its own.
- A **central "MCP Hub"** that acted as the coordinator — polling the agents,
  correlating their findings, deciding what to investigate next, and routing
  actions back out. (See the original topology and roadmap diagrams in
  [`../assets/`](../assets/): `original-mcp-hub-topology.png`,
  `original-logical-topology.png`, `original-detect-and-resolve.png`,
  `original-project-roadmap.png`.)
- A **framework bake-off** — AutoGen vs. CrewAI vs. LangChain vs. LangGraph —
  to pick the orchestration engine. The instinct was that the *framework* was
  the important decision.
- An elaborate **push-pull notification protocol** layered on top of MCP:
  event registration, fire-and-forget notifications, priority queues, sequence
  numbers, correlation IDs — all so the autonomous agents could proactively
  alert the Hub without waiting to be polled.

It was internally coherent. It was also, in hindsight, mostly a way to spend
enormous effort building machinery that the actual problem did not need.

### The two beliefs underneath it all

Everything above rested on two assumptions I would later discard:

1. **"More agents, each with a narrow role, will be more reliable than one
   general agent."** The intuition was separation of concerns — give each agent
   a small job and a small toolset and it will do that job well.
2. **"MCP is an agent-orchestration layer."** I conflated MCP (which is really a
   clean *tool/transport protocol*) with a *coordination brain*. So I designed a
   "Hub" whose job was to think, decide, and correlate across agents.

Both felt right at the time. Both turned out to be wrong for this problem.

---

## Part 2 — What contact with reality taught me

I got the chance to build a real instantiation of these ideas for a production
trading platform. The system I actually shipped diverged sharply from the plan
above — not because the plan was lazy, but because building it exposed costs the
diagrams never showed.

### Pivot 1 — From a society of agents to *one capable agent, many thin surfaces*

The multi-agent design produced exactly the failure modes the safeguards were
supposed to prevent. Layered agents:

- **multiplied token churn** — agents re-explained context to each other and
  re-derived the same conclusions;
- **second-guessed one another**, with one agent's output destabilizing the
  next;
- **failed in correlated ways** — when the underlying model misjudged something,
  *every* agent built on that misjudgment tended to misjudge it the same way, so
  the redundancy bought far less safety than it appeared to; and
- **were miserable to debug**, because a wrong answer could originate anywhere in
  a chain of handoffs.

What I built instead was a single, very capable agent harness behind a
centralized service (I came to call it the **LLM-PROXY**), with **thin,
interchangeable front-end surfaces** on top: a conversational web chat (with a
history slide-out), a one-shot completion endpoint, and a terminal `ask` command.
The surfaces are deliberately dumb. The proxy is the only brain.

### Pivot 2 — The "MCP Hub" dissolved entirely

Once there was one brain instead of six, the central coordinator had nothing left
to coordinate. The Hub — the centerpiece of the original architecture — simply
**ceased to have a reason to exist.** MCP went back to being what it actually is:
a great way to expose *tools* to the agent. Not a society of autonomous services.
Not an orchestration fabric. Just tools, behind a clean protocol, called by the
one agent when it needs them.

The elaborate push-pull notification protocol went with it. It was solving a
problem (autonomous agents proactively alerting a coordinator) that no longer
existed.

### Pivot 3 — From up-front role/tool partitioning to *on-demand tool discovery*

The original reason to split into role-scoped agents was partly to keep each
agent's tool list small — you can't dump hundreds of tool definitions into one
context. But the right solution wasn't to split the *agent*; it was to split the
*tool catalog*. I built a **tools-router**: tools are registered with metadata
(category, use-case, when-to-use), and the agent **discovers the relevant ones on
demand** rather than carrying every definition in its system prompt. One agent,
full reach, small working context. This solved the problem the six-agent split
was really trying to solve, without the coordination tax.

### Pivot 4 — The framework was not the important decision

I spent real time on the AutoGen/CrewAI/LangChain/LangGraph bake-off, believing
the framework choice was foundational. In practice, the heavy orchestration
frameworks mostly added abstraction I then had to fight. What mattered was a
**thin provider-abstraction layer** so the harness could talk to any model
(OpenAI, Anthropic, Google, Ollama), plus **good tracing** so I could see and
debug the agent's loop. The harness logic itself — the loop, the tool dispatch,
the context assembly — was better owned directly than inherited from a framework.

### Pivot 5 — From autonomous agents to a human-in-the-loop co-pilot

The original design imagined agents running continuously and acting on their own.
What proved genuinely valuable was an agent that is **always available and deeply
context-aware, but invoked by a human** — surfacing, correlating, and explaining,
with recommendations, while a person stays in the loop for anything consequential.
The agent never trades and never acts blindly on production; it makes the human
faster and more thorough. That framing also kept the security team comfortable,
which is what made adoption possible at all.

### Pivot 6 — The biggest win was one we never designed for

The capability that turned out to matter most wasn't on the original roadmap at
all: **incident root-cause analysis via a correlated event timeline.** Because
the one agent has broad knowledge of the platform (from a rich knowledge base)
*and* the ability to pull from live logs, dashboards, the trade database, and
cloud change-history, it can stitch a single, time-ordered narrative across all
of those sources — finding the needle in the haystack that a human would need
hours and many open tabs to assemble. This wasn't a *component* we built. It
*emerged* once the brain, the knowledge, and the tools all lived in one place.

---

## Part 3 — The lessons, distilled

For anyone standing where I stood at the beginning:

1. **Start with one capable agent, not a committee.** Reach for multiple agents
   only when you have concrete evidence of a bottleneck that a single context
   genuinely cannot hold — not on the assumption that division of labor buys
   reliability. With LLMs, it often buys correlated failure and token churn
   instead.
2. **Don't confuse a tool protocol with a brain.** MCP (and tools generally) are
   how the agent *reaches* the world. The reasoning belongs in one place. Don't
   build an orchestration hub you don't need.
3. **Split the tool catalog, not the agent.** On-demand tool discovery gives you
   broad capability with a small working context — the real goal behind most
   "let's add another specialized agent" impulses.
4. **The framework is not the foundation.** A thin model-provider layer plus
   solid tracing beats a heavyweight orchestration framework for most of this
   work. Own your loop.
5. **Keep a human in the loop and stay safe by default.** An always-available,
   context-aware co-pilot that *recommends* is more valuable — and far more
   adoptable — than an autonomous actor. Safe-by-default is what gets you past
   security review and into real use.
6. **Build the substrate; let the killer apps emerge.** The single most valuable
   capability (timeline-based RCA) wasn't designed up front. Put the knowledge,
   the brain, and the tools in one place, and capabilities you didn't plan for
   will surface on their own.

---

## What this repository is now

This repo is being rebuilt as an **open-source bootstrap** for the simpler,
practical design I arrived at: a centralized LLM-PROXY with a capable agent
harness, an on-demand tools-router, safe-by-default tooling, a knowledge-base
pipeline, and thin front-end surfaces — starting with a terminal `ask` command.

The original trading-platform specifics stay private; what's open is the reusable
*framework* and the patterns, so anyone can point it at their own systems and get
a practical DevOps/SRE co-pilot of their own.

See the [README](../README.md) for the current architecture and roadmap.
