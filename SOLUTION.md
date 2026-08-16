# Solution write-up — PSA Code Sprint 2.0

This documents the solution architecture, execution flow, key decisions, and the
impact / security / safety / scalability considerations required by the brief.

## Architecture

```
simulated world (fleet w/ tracking + containers w/ stow cells + pallet shipments)
        │
        ▼
   SQLite data layer (single source of truth for planner + dashboard)
        │
        ├──▶ MCC planning agent (tool layer → deterministic planner → plans + trace)
        │
        ▼
   Control tower dashboard (classic UI + Streamlit)
        ├── MCC Tracker — searchable incoming container dropdown, ship-tracker
        │                  sidebar (collapses the list to fit the detail on
        │                  screen), berth map
        ├── PSCH Plan — receiving / robot putaway / consolidation schedule
        ├── Control Tower — MCC pipeline KPIs
        └── Execution Trace — tool calls, decisions, inspections
```

One cargo story, two facilities, one shared view: the **Container Port**
(sealed containers, high volume, CITOS-style vessel/yard data) and the **PSA
Supply Chain Hub** (opened, stored, re-consolidated cargo). The agent's job is
to make the **MCC cargo handoff** between them visible and planned in advance.

## Execution flow

1. **Input** — the agent reads the shared state through a read-only tool layer:
   vessels (ETA/ETD/distance/speed), containers (stow cells, customs, cargo
   flag), and pallet shipments.
2. **Analyse** — it identifies every inbound MCC container and derives its full
   journey timeline from the carrying vessel's ETA: sea arrival → quay unload →
   port depot → road dispatch → **PSCH receipt ETA**.
3. **Plan receiving** — before the cargo arrives, it computes the inbound
   arrival rate over the next 6 hours and opens receiving areas accordingly,
   then assigns each container a dock door, a staging window, a robot putaway
   bin, and a release lane.
4. **Plan consolidation** — it groups the inbound containers by final
   destination, picks the vessel each outbound container is bound for (by
   destination and earliest ETD), and times the stuffing window against the
   vessel's loading cutoff. Each member container's **pallet pick time** is then
   set inside that window (never before the cargo itself arrives).
5. **Handling uncertainty** — customs-HELD and special-handling cargo is
   surfaced in the reasoning; every decision is deterministic and derives from a
   single source (the vessel ETA), so partial or noisy inputs degrade
   gracefully.
6. **Human review** — the dashboard is the human-in-the-loop surface: the
   planner sees the agent's full reasoning for every container and can override
   anything by re-planning or adjusting the scenario.
7. **Trace** — every tool call, decision, berth inspection and planner run is
   appended to an auditable execution trace.

## Key decisions

- **Deterministic agent, LLM-swappable.** The coordination brain is a
  rule-based planner so every demo is byte-for-byte reproducible with zero API
  cost; the tool layer is the seam where an LLM backend could be plugged in
  later. The agent still *orchestrates*: it reads multiple feeds through tools,
  reasons over them, and produces structured plans with explicit reasoning.
- **One source of truth for time.** Journey statuses are *derived* from the
  plan times rather than stored, so the world and the plan can never disagree.
- **Propose, never execute.** The agent writes only plans; the scenario is the
  only thing that mutates the world.
- **Synthetic, reproducible data.** All timestamps anchor to a fixed `SIM_NOW`,
  so demos and tests are deterministic. The seeded scenario is deliberately
  spread across all five journey stages at once, so the whole story is visible
  in a single screenshot.

## Impact

- **Visibility**: cargo owners and planners can answer "where is my MCC cargo
  and when does it reach PSCH?" for every container, end to end — the core of
  PSA's Node-to-Network strategy.
- **Planned, not reactive, receiving**: receiving areas and robot putaway bins
  are prepared from the inbound volume rate before trucks arrive, cutting
  staging congestion at PSCH.
- **Missed sailings reduced**: consolidation stuffing, pallet picks, and lane
  releases are scheduled backwards from each bound vessel's loading cutoff, so
  outbound containers are ready at the quay in time.
- **Dwell reduction**: the coordinated journey timeline keeps containers moving
  from vessel to PSCH doorstep instead of sitting in the port yard.

## Security & safety

- All data is synthetic; no real CITOS/PORTNET systems are touched.
- The agent is advisory by default — it proposes plans and writes nothing that
  executes in a real operation.
- Every decision is recorded in the execution trace for audit.
- No destructive operations: the app only reads/updates a local SQLite file.

## Scalability

- The data layer is a plain SQLite repository with a thin schema; it can be
  swapped for a real message stream (AIS/ETA feeds, PORTNET/CITOS adapters)
  without changing the planner or dashboard contracts.
- The planner reads state through tools rather than one giant prompt, so
  context stays bounded as the scenario scales.
- The propose–review–execute pattern is reusable: the same harness can be
  extended to topping-up, yard slotting, or other agentic decisions at PSCH.
