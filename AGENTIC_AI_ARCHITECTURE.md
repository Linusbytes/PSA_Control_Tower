# Agentic AI Architecture — PSA iWX (Tuas Port ↔ PSCH)

> This document is the design contract for making the software **agentic-AI-ready**:
> how the external agentic AI API plugs in, how the additional container-freight-
> station (CFS) functions become agent capabilities, and how PSCH and Tuas Port
> integrate through one shared layer. The seam described here is implemented in
> `agents/tools.py`, `agents/brain.py` and `agents/runtime.py`.
>
> **Every agentic AI integration point across the whole software, with feasibility
> ratings, is tracked in `AGENTIC_AI_INTEGRATION_MAP.md`.** That map is a living
> checklist: any feature work must add its AI integration areas there.

## 1. The core principle: a swappable brain behind one seam

The software is built so the **decision engine** (the "brain") is
interchangeable without touching the data layer, the tools, or the dashboard:

```
goal / operational event
        │
        ▼
   AgentRuntime  ──►  AgentBrain (protocol)          ← the seam
        │                  │  RuleBasedBrain (today, deterministic)
        │                  │  AgenticAPIBrain (external agentic AI API)
        │                  ▼
        │          agents/tools.py: ToolRegistry
        │          call_tool(name, args)  ──►  data/store.py (SQLite)
        │                  │
        │                  ▼
        │          execution trace (auditable record of every read/write)
        ▼
   dashboard (human-in-the-loop review)
```

* **`agents/tools.py`** — a declarative registry of every capability the agent
  can use, each with a name, description, JSON parameter schema, and a
  **permission level** (`read` / `mutate` / `approval`). `tool_schemas()`
  serialises the registry into OpenAI-style function definitions.
* **`agents/brain.py`** — the `AgentBrain` protocol. `RuleBasedBrain` wraps the
  deterministic MCC planner (zero API, reproducible). `AgenticAPIBrain` drives
  the external agentic AI API: it sends the goal + context + `tool_schemas()`,
  executes the model's tool calls through `call_tool`, and honours permission
  gates. `default_brain()` picks one from config.
* **`agents/runtime.py`** — the orchestration loop every agent runs through. It
  records each run in the trace and applies the **autonomy gate** (advisory /
  semi-autonomous / autonomous) so execution-level actions always route to a
  human in advisory mode.

### How to plug in the agentic AI API

1. Set `AGENTIC_API_ENDPOINT` (any OpenAI-compatible agentic endpoint that
   supports function calling) in `.env`; optionally `AGENTIC_API_KEY` and
   `AGENTIC_API_MODEL`.
2. `AgenticAPIBrain` takes over automatically — the runtime, tools, trace and
   dashboard are unchanged.
3. The agentic loop is provider-agnostic: system prompt + `tool_schemas()` →
   model returns tool calls → **read** tools are executed via `call_tool`;
   **mutate and approval tools are never executed by the agent** — they are
   returned as `pending_approval` for a human (`AgentRuntime.approve()`) →
   results fed back → repeat until the model stops calling tools (bounded by
   `AGENTIC_MAX_TOOL_ITERATIONS`). The whole-batch plan writers are hidden from
   the LLM (`expose_to_llm=False`), so it can only propose granular changes.
4. If the endpoint is unreachable or unset, `default_brain()` falls back to the
   rule-based planner, so the demo never breaks.

## 2. Agent capabilities across the whole CFS

PSCH at Tuas manages more than MCC. Each function is a **goal the same runtime
can run**, reusing the same tool registry and cargo record. Adding a function
means adding tools (if new state is needed) and a brain/goal — not a new
system.

| CFS function | Agent goal | New state / tools it needs | Autonomy profile |
|---|---|---|---|
| **Multi-Country Consolidation (MCC)** | Plan the full journey: vessel ETA → PSCH receipt → receiving/putaway → consolidation → outbound vessel | (implemented) vessels, containers, stowage, shipments, bins, lanes | advisory (proposes plans) |
| **Full Container Load (FCL)** | Track & hand off a shipper-exclusive container; alert on ETA/cut-off/customs so it clears the port → PSCH (or direct) on time | FCL containers flagged `import`/`export` (already in schema); add FCL handoff events + cut-off alerting | advisory; near-autonomous for pure status updates |
| **LCL deconsolidation** | Plan unstuffing of inbound LCL containers: dock door, staging, putaway, then route cargo to local delivery *or* re-consolidation | unstuff work orders, dock-door schedule, per-shipment routing decision | advisory |
| **Local LCL Delivery** | Plan dispatch of unstuffed LCL cargo: vehicle allocation, route sequencing, ETA, and capture proof of delivery | delivery orders, truck fleet (drayage table exists), route/POD records | semi-autonomous (dispatch is low-risk, reversible) |
| **Transloading** | Raise a transload work order on container-to-container transfer, record the reason (damaged container, relabelling, weight rebalance…), track the rework, and recover cost | transload work orders, damage/inspection records, reason codes, cost capture | advisory — `approval` gate on releasing a replacement container |
| **Container topping-up** | Suggest cargo/volume to top up under-filled containers before sailing | utilisation of in-progress outbound containers (derivable from outbound + shipments) | advisory |

Because every agent runs through `AgentRuntime`, the **execution trace** is the
single audit trail across all functions — the hackathon brief's requirement #6
holds for the whole facility, not just MCC.

## 3. Linking features across the software

Three mechanisms keep the functions from becoming silos:

1. **One cargo record.** A container → its shipments (pallets) → the bin →
   the consolidation group → the outbound container → the vessel. LCL
   deconsolidation writes the shipments that Local LCL Delivery later
   dispatches; MCC reads the same shipments to build outbound containers. No
   function re-keys another's data.
2. **One tool registry.** Every read/write flows through `call_tool`, so any
   brain (rule-based or LLM) can reach any function's data with the same
   schema. Cross-function orchestration is just a goal that calls tools from
   several functions in sequence.
3. **One event/trace stream.** `trace` records every tool call, decision and
   approval. A future reactive layer can consume the same stream — e.g. a
   "cargo arrived at PSCH" event triggers the LCL delivery planner, a
   "cut-off approaching" event triggers the MCC re-plan.

## 4. PSCH and Tuas Port integration

The internal data model is the **contract**; external systems attach through
thin adapters. Today everything is simulated (`data/simulator.py`); a
production build swaps each simulator feed for a real client without changing
the planner, tools, or dashboard.

| External system | What it provides | Adapter |
|---|---|---|
| **Tuas Port — CITOS** (terminal operating system) | discharge events, yard slots, quay plan, vessel berth windows | `integrations/citos` → writes `containers`/`vessels`/`yard_status` |
| **PORTNET / CALISTA** (community/EDI layer) | bookings, documentation, track-and-trace, haulier appointments | `integrations/portnet` → writes `bookings`, gate-out events |
| **TradeNet** (customs) | declaration status → customs `cleared/held` | `integrations/tradenet` → updates `customs_status` |
| **AIS / VTMS** (marine) | live ETA, distance, speed → the ship-tracker fields | `integrations/ais` → updates `vessels.distance_nm/speed_knots/eta` |
| **PSCH WMS** (warehouse) | receiving, putaway, picking confirmations | already the core `store` tables (`shipments`, bins, lanes) |
| **PSCH robots / AGVs** | move confirmations, bin occupancy | `integrations/psch_equipment` → confirms `move_start/move_end`, bin `occupied` |

Design rules:

* **Ports are adapters, not endpoints.** The agent and dashboard only ever
  speak to `data/store.py`; an adapter maps an external schema into it. Swapping
  the simulator for CITOS/PORTNET is a drop-in replacement.
* **The handoff is the integration point.** The highest-leverage seam between
  the Port and PSCH is MCC/LCL cargo handoff — the journey timeline
  (`sea_arrival → … → psch_receipt_eta`) is exactly the contract an adapter
  must keep fresh.
* **Events, not polling.** Production adapters push events (discharge done,
  gate-out, customs cleared) into the trace/event stream; the runtime reacts
  rather than re-plans on a timer.

## 5. Autonomy & human-in-the-loop

The brief allows advisory → human-in-the-loop → autonomous. This build is
**advisory by default**:

* `read` tools: always allowed, fully traced.
* `mutate` tools (plan proposals): applied and shown for review in the
  dashboard; in `advisory` mode they are gated, in `semi_autonomous`/
  `autonomous` they auto-apply.
* `approval` tools (real-world execution: releasing a lane, dispatching a
  truck, booking a port slot): never executed by the agent in advisory or
  semi-autonomous mode — `AgentRuntime.approve()` is the human's action.

Higher autonomy is *not* automatically better; the table in §2 picks the level
that fits each function's risk.

## 6. File map

```
agents/tools.py       declarative tool registry + call_tool + tool_schemas
                      (granular change tools: reassign_bin, reschedule_receiving_area,
                      release_lane; whole-batch writers are expose_to_llm=False)
agents/brain.py       AgentBrain protocol, RuleBasedBrain, AgenticAPIBrain (seam)
                      — the LLM loop returns every mutate/approval tool as
                      pending_approval; it never executes plan changes itself
agents/runtime.py     AgentRuntime: run loop, permission gates, approve(), trace
agents/mcc_planner.py the deterministic MCC brain (reads/writes via call_tool)
agents/intel.py       PSA Intelligence rule brain (Q&A + plan-change proposals)
agents/exception.py   exception agent (scan_exceptions / find_exceptions /
                      ExceptionBrain) + the road-delay layer's consumer
config.py             AGENTIC_API_* + AGENTIC_AUTONOMY settings
AI_INTEGRATION_ROADMAP.md   phased plan (0–4 done), AGENT_ONBOARDING.md the
                            implementation map, AGENTIC_AI_INTEGRATION_MAP.md
                            the living integration checklist
```

## 7. Built status (Phase 0–4 of AI_INTEGRATION_ROADMAP.md)

- **Phase 0** — PSA Intelligence page + `IntelRuleBrain` (`/api/intel/ask`).
- **Phase 1** — `POST /api/agent/run` + toolbar "Run Agent Goal" button.
- **Phase 2** — `AgenticAPIBrain` live against a local Ollama server
  (`qwen2.5:7b`); config-flip only.
- **Phase 3** — exception agent + live Exception Watch panel on the Control
  Tower (`/api/agent/exceptions`).
- **Phase 4** — granular change tools + Approve/Reject UI
  (`/api/agent/approve`, `/api/agent/reject`); the LLM can propose but never
  execute a plan change, and cannot see the whole-batch plan writers.
- **Phase 5** — remaining: live-data polish (e.g. tool-trace disclosure under
  answers) + demo video.
