# PSA Code Sprint 2.0 — Hackathon Context (reference for building this project)

> This file records the competition brief and how the project in `PROJECT_SPEC.md`
> maps to it. It is a context/reference document only — it does not define the
> technical build. See `PROJECT_SPEC.md` for that.

## The competition

**PSA Code Sprint 2.0: Agentic AI in Action**

Teams identify a problem relevant to PSA and build an agentic AI solution that can
**reason, make decisions, and coordinate actions toward a defined objective**.

### Autonomy level

Solutions may be **advisory, human-in-the-loop, or autonomous**. Higher autonomy
is **not automatically better** — teams should pick the level that fits the use
case, operational risk, and available controls.

> Our project is **advisory / human-in-the-loop**: the MCC planner proposes the
> full journey, receiving, and consolidation plan with explicit reasoning; a
> human planner reviews it in the dashboard. Nothing executes automatically.

### What the solution must demonstrate

Process inputs such as an event log, state change, operational alert, process
metric, or user request, and show it can:

1. **Analyse input** and identify the objective or issue
2. **Determine an appropriate course of action**
3. **Orchestrate** relevant tools, systems, or workflows
4. **Handle uncertainty**, incomplete information, and tool failures
5. **Invoke human review, approval, or escalation** where appropriate
6. **Produce a clear execution trace** covering key decisions, tool calls,
   approvals, actions, results, and errors

## How this project fits the brief

The project is an **MCC (multi-country consolidation) control tower** spanning
the Tuas Container Port and the PSA Supply Chain Hub (PSCH):

- **Inputs:** simulated vessel tracking (ETA, distance, speed), container
  stowage cells (Bay-Row-Tier), discharge/depot state, and PSCH pallet
  shipments.
- **Objective/issue:** MCC cargo's journey from vessel to PSCH doorstep and back
  onto an outbound vessel is a black box; receiving is planned reactively;
  consolidation isn't synced to vessel cutoffs → dwell, congestion, missed
  sailings.
- **Course of action:** derive each container's journey timeline and PSCH
  receipt ETA from the vessel ETA; plan receiving areas, robot putaway bins,
  pallet pick times and lane releases; schedule each outbound consolidation
  container against a specific vessel's loading cutoff.
- **Orchestration:** the agent reads the shared data layer through a read-only
  tool layer (every call traced), reasons over multiple feeds, and writes
  structured plans.
- **Human-in-the-loop:** the dashboard is the review surface — reasoning for
  every container is shown, and the planner can regenerate/re-plan at any time.
- **Execution trace:** tool calls, agent decisions, consolidation groups, and
  berth inspections are all recorded.

### The demo story (what the judges see)

1. **Incoming Containers** — a search + dropdown of MCC container numbers (filter
   by container number, vessel, status, berth or destination); picking one
   collapses the list so the map slides left and the detail panel widens.
2. **Ship-tracker sidebar** — which vessel carries it, the voyage ID, how far
   the ship is from Tuas, how fast it travels, its berth at Tuas, and the
   container's exact Bay-Row-Tier cell in the moving vessel.
3. **Agent derives the journey** — statuses roll through *En Route (Sea) →
   Unloaded → Depot → En Route (Road) → Arrived* with the ETA at the PSCH
   doorstep.
4. **Receiving is planned before arrival** — receiving areas opened by inbound
   volume rate; robot putaway bins assigned.
5. **Full PSCH process plan** — arrival, staging wait, move start, bin
   location, pallet pick time (driven by when the outbound vessel arrives to
   load), and lanes released per container number.
6. **Outbound "Loaded"** — the new container shows *Loaded* with the vessel it
   is bound for; the vessel's **berth rectangle pops up on the map**, with when
   the vessel leaves the port, the exact loading cell on the vessel, and the ETA
   to arrive at the quay loading area.

## Deliverables (each team must submit)

| Deliverable | Limit |
|---|---|
| Demonstration video | up to 10 mins |
| Presentation slides | up to 10 slides |
| Explanation of solution architecture, execution flow, key decisions, potential impact, and security/safety/scalability considerations | — |

## Deliverable mapping / status tracking

- [ ] Demo video (≤10 min) — script: the six-scene story above
- [ ] Slides (≤10) — one per scene + architecture + impact
- [ ] Architecture / execution flow / impact / security-safety-scalability
      write-up (see `PROJECT_SPEC.md` §4 and `SOLUTION.md`)
- [x] Build complete — MCC world generator, planning agent, control-tower
      dashboard (ship tracker, berth map, PSCH plan, KPIs, trace), tests

## Key messages to remember when presenting

- Built on PSA's **Node to Network (N2N)** strategy — connecting siloed
  operational nodes into an integrated, digitally visible network.
- Port = sealed containers, high volume, mature optimization (CITOS); PSCH =
  opened/stored/consolidated cargo, judgment-heavy → **better fit for agentic
  reasoning**.
- Highest-leverage integration point: the physical/informational **handoff of
  MCC cargo** between the two facilities.
- The agent makes one big coordination decision visible and planned in advance:
  *when* the cargo reaches PSCH, *where* it is put away, *when* its pallets are
  picked, and *which vessel* it sails on.
- All data is **synthetic**, modelled on realistic structures — no real
  CITOS/PORTNET access.
