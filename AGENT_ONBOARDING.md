# Agent Onboarding — PSA Control Tower (Tuas Port ↔ PSCH)

> **START HERE if you are a new thread/agent.** This document is the
> implementation map: what the project is, how the code is organised, how data
> flows, how the **agent system operates today**, and where to make common
> changes. Read `README.md` for the user-facing story, then come back here.
> The `HACKATHON_BRIEF.md` is the competition brief this project answers;
> `AGENTIC_AI_ARCHITECTURE.md` is the design contract for the AI seam;
> `AI_INTEGRATION_ROADMAP.md` tracks the phases (0–4 done);
> `AGENTIC_AI_INTEGRATION_MAP.md` is the living AI-integration checklist.

---

## 0. Current state — handoff snapshot (updated 2026-08-18)

Everything below is **implemented, tested (92 tests pass) and verified live on
port 8513** (server: `python server.py`).

**The world:** a self-running synthetic "terrarium". Seeded deterministically
(`SIM_SEED=42`, 400 containers, 5 flows), it advances at `SIM_SPEED=60`× and
auto-regenerates a new wave when one lifecycle completes — no buttons needed.

**The agents (all on one runtime, `agents/runtime.py`):**

| Agent | File | Purpose |
|---|---|---|
| Planner | `agents/mcc_planner.py` (via `RuleBasedBrain`) | Derives every container's journey, receiving area, putaway bin/stacker, consolidation group and outbound loading cell. Writes proposals. |
| Q&A | `agents/intel.py` (`IntelRuleBrain`) → **PSA Intelligence page** | Answers anything about live data (containers, vessels, warehouse, pipeline, exceptions, plans). When `AGENTIC_API_ENDPOINT` is set it swaps to the LLM (`AgenticAPIBrain`). |
| Exception | `agents/exception.py` (`ExceptionBrain`) → **Attention needed panel** | Scans live state for receipt-ETA slips, customs holds, loading-window risk, vessel ETD slips; proposes fixes. |
| API (LLM) | `agents/brain.py` (`AgenticAPIBrain`) | Drives any OpenAI-compatible endpoint with function calling. Currently wired to a **local Ollama server** (`qwen2.5:7b`) via `.env` — see §7.3. |

**The human-in-the-loop loop (Phase 4):** the LLM (and the rule brain) can
*propose* granular plan changes (`reassign_bin`, `reschedule_receiving_area`,
`release_lane`) but **never execute them** — the PSA Intelligence page shows
**Approve / Reject** buttons and the change only lands after approval (then
every page reflects it within one 8 s poll). Whole-batch plan writers
(`save_mcc_plans`, `save_outbound_containers`) are **hidden from the LLM**
(`expose_to_llm=False`) so it cannot rewrite the plan set.

**What was just built (the last big change set):**
1. `get_terminal_snapshot` extended with deterministic vessel-by-status,
   containers-by-flow, outbound-by-destination, overdue-receipt and
   near-loading counts (stops the 7B model inventing numbers).
2. **Phase 1** — `POST /api/agent/run` + the toolbar goal bar (**▶ Run**)
   button (any goal through the runtime; traced; the toolbar now sends through
   the shared PSA Intelligence thread via `/api/intel/ask`).
3. **Phase 3** — exception agent + live **Attention needed** panel on the
   Control Tower; `POST /api/agent/exceptions`; "Ask the agent"
   shortcut.
4. **Phase 4** — granular change tools + **Approve/Reject** flow;
   `POST /api/agent/approve` and `POST /api/agent/reject`; the LLM brain now
   gates *every* mutate/approval tool as `pending_approval`.
5. A **deterministic road-delay layer** (`delay_hours` on ~12% of MCC plans,
   1–8 h) so the synthetic world genuinely produces exceptions for the
   exception agent to catch; `journey_status` uses the delayed arrival so the
   whole dashboard stays consistent.

**What is left:** Phase 5 of `AI_INTEGRATION_ROADMAP.md` — live-data polish
(e.g. a tool-trace disclosure under each PSA Intelligence answer) and recording
the demo video. The rule brain runs instantly; the LLM path takes ~30–60 s on
the first call after a server restart (model reload), then is fast.

---

## 1. What this project is

A **simulated control tower** that coordinates cargo between Tuas Container Port
and the **PSA Supply Chain Hub (PSCH)** — a container freight station (CFS) /
distribution centre in Singapore. Everything is **synthetic, seeded, and
deterministic**: there is no live port data, no GPS, no real vessels. The
software is a self-contained "terrarium" that tells one continuous story:

1. A vessel is inbound to Tuas with containers stowed in its bay plan.
2. Containers destined for PSCH are discharged, dwell in the depot yard, and
   are trucked (drayage) to PSCH.
3. At PSCH they are received at a numbered door, unloaded, and their pallets
   are put away into rack bins by **AS/RS stacker robots**.
4. Cargo dwells in storage, then is picked and either re-packed into an
   outbound container that returns to the port to catch a specific vessel
   (MCC), delivered by truck (LCL/FCL), topped up (Top Up), or transloaded.
5. When the whole wave has finished its lifecycle, the server **automatically
   seeds the next wave** — the ecosystem keeps living forever.

The purpose is a **hackathon demo** (PSA Code Sprint 2.0: Agentic AI in Action):
an agentic-AI coordination layer, advisory / human-in-the-loop, with a full
audit trail. It is built with a **swappable brain** seam so an external LLM
agent can replace the default rule-based planner via configuration.

## 2. The five service flows (every PSCH container is tagged with one)

| Flow | Code | What happens | Where in UI |
|---|---|---|---|
| Multi-country consolidation | `mcc` | Cargo deconsolidated, grouped by destination, re-packed into an outbound container that catches a specific vessel | Inbound → Storage → Outbound ▸ MCC |
| Less-than-container-load | `lcl` | Cargo broken into delivery units by land destination, trucked out | Inbound → Storage → Outbound ▸ Distribution |
| Full-container-load | `fcl` | Whole container received, staged, released by truck (never unpacked) | Inbound → Outbound ▸ Distribution |
| Top up | `topup` | Partially-filled container receives extra cargo, sealed, released | Inbound → Outbound ▸ Top Up |
| Transload | `transload` | Cargo moved container-to-container without long-term storage | Inbound (transfer bays in Storage) |

UI display categories collapse these into four filter chips: **MCC ·
Distribution (lcl+fcl) · Top Up · Transload**. Default mix for 400 containers:
~36% mcc, ~26% lcl, ~16% fcl, ~14% topup, ~8% transload.

## 3. Architecture at a glance

```
                ┌────────────────────────────────────────────────┐
                │                 config.py                      │
                │  SIM_NOW · SIM_SPEED (live clock) · sizing ·   │
                │  AGENTIC_API_* · AGENTIC_AUTONOMY              │
                └────────────────────────────────────────────────┘
                                   │
        ┌──────────────────────────▼──────────────────────────┐
        │                  data/simulator.py                  │   generates the world
        │  vessels · bay plans · containers · shipments ·     │   from SIM_SEED
        │  yard · drayage — five flows, 24/7, no shifts       │
        └──────────────────────────┬──────────────────────────┘
                                   ▼
        ┌─────────────────────────────────────────────────────┐
        │                 data/store.py (SQLite)              │   ONE SOURCE OF TRUTH
        │  containers · vessels · shipments · stowage ·       │   data/port.db
        │  mcc_plans · outbound_containers · trace · yard     │
        └──────────────┬──────────────────────────┬───────────┘
                       │                          │
        ┌──────────────▼─────────────┐   ┌────────▼──────────────────────┐
        │   agents/ (the brains)     │   │   server.py · dashboard/app.py │
        │  mcc_planner.py   rules    │   │   classic UI + JSON API        │
        │  brain.py         seam     │   │   Streamlit UI                 │
        │  runtime.py       loop     │   │   analysis/ (kpis, psch_view,  │
        │  tools.py         registry │   │   bayplan, facility)           │
        └────────────────────────────┘   └────────────────────────────────┘
```

Key architectural decisions:

- **One source of truth.** Everything lives in SQLite (`data/port.db`). Both
  UIs and the planner read the same store, so pages can never disagree.
- **Statuses are derived, never stored.** Journey stages, stacker charges,
  outbound statuses, KPIs are all computed *on read* from plan times against
  the **live sim clock** (`sim_now()`). Nothing is written except plans and
  raw scenario data.
- **The agent plans; it does not execute.** Plans are proposals in the store.
  Real-world actions would be `approval`-level tools, gated by the runtime.
- **Deterministic by default.** Same `SIM_SEED` → same world. `SIM_SPEED=0`
  freezes the clock → byte-for-byte reproducible runs.

## 4. The live clock (read this before touching any timestamp)

`config.py` defines the simulation clock:

- `SIM_NOW` — the anchor instant (`2026-08-16T12:00Z`).
- `SIM_SPEED` — sim-seconds per real second, default **60** (one sim hour per
  real minute). `0` = frozen (tests).
- `sim_now()` — returns the current sim instant = `SIM_NOW + elapsed_real *
  SIM_SPEED`. **All status derivation must use this**, never `datetime.now()`
  and never the raw `SIM_NOW` constant.

Everything that should "move" in real life is re-derived from `sim_now()` on
each state poll (the UI refreshes every 8 s):

- `agents/mcc_planner.py: journey_status() / outbound_status()` — journey
  stages roll forward as the clock passes plan times.
- `server.py: _asrs_state()` — the 8 stackers run a staggered charge rotation:
  charge drains ~8%/hour while working, parks at 45%, refills ~30%/hour at the
  charging station (2 bays). The rotation is pure math from the live clock.
- `server.py: build_state()` — calls `_maybe_regenerate_wave(now)` on every
  poll: when the current wave's lifecycle is complete (all pallets picked and
  all outbound containers loaded out), it **re-seeds the world relative to the
  live clock and re-runs the planner** — the terrarium behaviour.

So the answer to "do the metrics update in real time?" is: **yes, automatically
— no toggle.** The page polls every 8 seconds and every status is re-derived
from the advancing clock. The only knobs are `SIM_SPEED` (pace) and the
`⟳ Regenerate` / `▶ Run Hub Planner` buttons (manual reset).

## 5. Module-by-module map

### `config.py` (~136 lines)
All knobs: sim clock (`SIM_NOW`, `SIM_SPEED`, `sim_now()`), scenario sizing
(`SEED`, `N_CONTAINERS=400`, `DRAYAGE_TOTAL=12`), MCC journey durations
(`UNLOAD_MIN`, `DEPOT_DWELL_MIN`, `ROAD_TRANSIT_MIN`, ...), PSCH capacity
(`PSCH_RECEIVING_LANES=10`, `PSCH_AISLES_TOTAL=24`, `PSCH_RELEASING_LANES=40`,
...), and the agentic AI settings (`AGENTIC_API_ENDPOINT/KEY/MODEL`,
`AGENTIC_AUTONOMY`, `AGENTIC_MAX_TOOL_ITERATIONS`). Reads a `.env` if present.

### `models/schemas.py`
Pydantic models — the data contract. Key types: `Container` (has `flow`,
`destination`, `stow_bay/row/tier`), `Vessel`, `Booking`, `YardStatus`,
`DrayageStatus`, `SlaProfile`, `MccPlan`, `OutboundContainer`, plus enums
(`CargoFlag`, `CustomsStatus`, `ServiceType`, `StorageZone`, `VesselStatus`,
`JourneyStatus`, `OutboundStatus`).

### `data/simulator.py` (~730 lines)
The world generator. `generate(seed, n_containers, sim_now)` returns a
`Scenario` (vessels, bay plans/stowage, containers, bookings, shipments, yard,
drayage) deterministically from the seed. Ships are spread 24/7 (no shifts).
Container sizes are 20FT/40FT/40HC with correct stowage conventions (heavy low,
reefer at power stacks, DG segregated, OOG on top, 20ft in odd bays).

### `data/store.py` (~705 lines)
The SQLite data layer. Tables: `containers`, `bookings`, `yard_status`,
`vessels`, `drayage`, `sla_profiles`, `shipments`, `mcc_plans`,
`outbound_containers`, `vessel_stowage`, `trace`. Functions are thin
get/save helpers (`get_containers`, `save_mcc_plans`, `record_event`,
`get_trace`, ...). **Every agent state read/write should go through this
layer** (and preferably through the tool registry, §7).

Granular in-place updates (the Phase 4 change tools):
- `update_mcc_plan(path, container_id, **fields)` — writes only whitelisted
  `MCC_PLAN_EDITABLE_FIELDS` (`bin_location`, `receiving_area`,
  `putaway_robot`, `release_lane`, `consolidation_group`, `reasoning`).
- `update_outbound_container(path, container_id, **fields)` — whitelisted
  `OUTBOUND_EDITABLE_FIELDS` (`status`, `loading_lane`, `lane_release_time`,
  `reasoning`).
- `_migrate()` — adds columns to older DBs (`flow`, `destination`,
  `delay_hours`); `server.py main()` calls `init_db` on every start so
  existing `data/port.db` files get new columns automatically.

### `data/facility.py` (~430 lines)
The PSCH physical model: bin naming (`AISLE-LEVEL-BAY`), room/aisle layout
(ambient 1–21 with aisle 21 DG-segregated, cold room 22–24), bin iteration,
and `build_psch_space()` which produces the space-utilisation view used by
both UIs.

### `data/seed.py`
CLI/entry: `seed()` generates + loads a scenario and records
`scenario_seeded`; `seed_if_empty()` used by server startup.

### `agents/mcc_planner.py` (~680 lines)
The deterministic planning agent — the current default brain. `plan()` derives,
for every inbound container: journey timeline (sea → unload → depot → road →
PSCH), receiving area/door, putaway bin + stacker (slotting rule: fast movers
at floor level), consolidation grouping by destination/cutoff, releasing lanes
(contiguous spans), and outbound container ↔ vessel loading cells. It reads and
writes through `agents.tools` so every call is traced.

**Road-delay layer:** `_delay_hours(cid)` gives ~12% of plans a deterministic
`delay_hours` (1–8 h) — the world fact that makes the exception agent
meaningful. `journey_status()` treats `psch_receipt_eta` as the *promised*
time and `psch_receipt_eta + delay_hours` as the *actual* arrival, so every
page shows the delay consistently. The promise is kept visible (the exception
detail shows both times).

### `agents/tools.py` (~535 lines)
The declarative **tool registry** — the agentic AI seam's vocabulary. Every
tool has a name, description, JSON parameter schema, permission level
(`read` / `mutate` / `approval`), a handler, and an `expose_to_llm` flag
(default True). `tool_schemas()` serialises them into OpenAI-style function
definitions — **skipping `expose_to_llm=False` tools** (the whole-batch plan
writers, rule-planner-only). `call_tool()` is the single funnel for all agent
tool use and records every call to the trace.

Read tools: `list_vessels`, `list_containers`, `list_shipments`,
`list_vessel_stowage`, `get_mcc_plans`, `get_outbound_containers`,
`get_bookings`, `get_yard_status`, `get_drayage`, `get_slas`, `get_psch_space`,
`get_trace_events`, and **`get_terminal_snapshot`** — the authoritative KPI
snapshot (journey stages, `containers_at_psch_now`, overdue receipts,
vessels-by-status, containers-by-flow, outbound-by-status *and*
by-destination, near-loading count, bin/yard utilisation). The LLM system
prompt instructs the model to answer count/status questions from this tool
rather than deriving numbers from raw tables (a 7B model otherwise
hallucinates counts from percentages).

Mutate tools (rule-planner-only, `expose_to_llm=False`): `save_mcc_plans`,
`save_outbound_containers` — whole-batch plan writers.

Granular change tools (the only plan writers the LLM can see):
- `reassign_bin(container_id, bin_location, reason)` (mutate) — updates only
  that plan's `bin_location` (validated against `data/facility.is_bin`,
  AISLE-LEVEL-BAY e.g. `5-08-1B`) and appends a reasoning line.
- `reschedule_receiving_area(container_id, receiving_area, reason)` (mutate).
- `release_lane(container_id, reason)` (approval) — sets
  `lane_release_time=now` so the outbound status advances staged → released.

### `agents/brain.py` (~300 lines)
The swappable-brain seam. `AgentBrain` protocol → `RuleBasedBrain` (default,
delegates to `mcc_planner`) and `AgenticAPIBrain` (drives any
OpenAI-compatible agentic endpoint with function calling). `default_brain()`
picks from config: if `AGENTIC_API_ENDPOINT` is set, the API brain takes over;
otherwise the rule brain runs so nothing ever breaks.

**`AgenticAPIBrain` loop:** system prompt (grounding rules + goal + context) +
`tool_schemas()` → model returns tool calls → reads execute via `call_tool`;
**any `mutate`/`approval` tool is returned as `pending_approval` (never
executed inside the brain)** and recorded as `approval_required` in the trace;
the model is told its proposal awaits a human. Bounded by
`AGENTIC_MAX_TOOL_ITERATIONS` (20). `_system_prompt()` contains the grounding
rules: prefer `get_terminal_snapshot` for counts, never invent numbers, never
rewrite whole plan batches.

### `agents/runtime.py` (~145 lines)
`AgentRuntime` — the orchestration loop every agent runs through: records the
run in the trace, applies the **autonomy gate** (`advisory` / `semi_autonomous`
/ `autonomous`), and exposes `approve()` for human approval of execution-level
actions. `requires_approval()` is where the permission model lives: `read`
always allowed; `mutate` gated in `advisory`; `approval` gated unless
`autonomous`.

### `agents/intel.py` (Phase 0 — PSA Intelligence rule brain)
`IntelRuleBrain` answers questions from live data through the tool registry:
container tracking, plan reasoning, warehouse state, pipeline counts, flows,
vessels, outbound, exceptions, recent trace, help, and **plan-change
proposals** (`_propose_change` — "move X to bin 5-08-1B" returns a
`pending_approval` event instead of executing). `default_intel_brain()` returns
`AgenticAPIBrain` when `AGENTIC_API_ENDPOINT` is set, else the rule brain.

### `agents/exception.py` (Phase 3 — exception agent)
`scan_exceptions(plans, outbounds, containers, vessels, now)` — **pure**, no
tool calls (used by the 8 s poll in `build_state` so the trace is never
flooded). `find_exceptions(path, now)` — traced version for agent runs
(`ExceptionBrain` uses it). Detects: receipt ETA missed (road-delay slips),
customs holds, loading-window missed/approaching, vessel ETD slips — each with
severity (`critical`/`warning`), issue, detail and recommendation.

### `agents/run.py`
Headless CLI: seed if empty, run the planner, print the plan.

### `analysis/` (view helpers, shared by both UIs)
- `kpis.py` — `compute_kpis()`: pipeline totals, journey counts, outbound
  statuses, PSCH hub flows, yard/drayage utilisation.
- `bayplan.py` — vessel bay-plan visualisation (Bay-Row-Tier cross-section).
- `psch_view.py` — PSCH facility HTML/CSS helpers (`FACILITY_CSS`,
  `facility_html`, `lanes_html`, `bin_grid_html`, ...). The receiving/releasing
  lane chips live here (`_lane_chip_html`), with nowrap + horizontal scroll.
- `data/facility.py` `build_psch_space` feeds it.

### `server.py` (~4070 lines)
The **classic UI** (early-2000s grey/navy bevel styling) + stdlib JSON API —
this is the full hub experience. Big moving parts:

- `build_state()` — assembles the entire JSON view state from the store + live
  clock; also the terrarium hook (`_maybe_regenerate_wave`) and the live
  **exception watch** (`scan_exceptions` over data already in hand — never
  traced, so the 8 s poll doesn't flood the trace).
- `_asrs_state()`, `_berth_state()`, `_wave_complete()` — derivations.
- `PAGE` — the whole HTML/CSS/JS page as one string (the UI logic is inline
  JavaScript: `renderPsch()`, `renderPschRcvLanes()`, `renderPschAsrs()`,
  `renderTower()` (KPI strip + Exception Watch), `renderTrace()`, the
  **PSA Intelligence** chat page (`renderIntel`/`intelSend`/`intelApprove`/
  `intelReject`), `toolbarAsk()` — the toolbar goal bar shares the intel thread).
- `Handler` — HTTP GET: `/`, `/api/state`, `/api/health`, `/map.png`,
  `/static/*`; POST: `/api/seed`, `/api/agent/plan`, `/api/agent/run`,
  `/api/agent/exceptions`, `/api/agent/approve`, `/api/agent/reject`,
  `/api/intel/ask`, `/api/trace/clear`, `/api/berth/inspect`,
  `/api/psch/inspect`, `/api/bayplan`.

### `dashboard/app.py` (754 lines)
Alternative **Streamlit** UI (tabs: Track / Plan / Space / Tower / Trace).
Shares the same store and agent; not restructured to the hub's page layout.

### `tests/` — 92 tests
`test_simulator.py`, `test_facility.py`, `test_mcc_planner.py`,
`test_frontend.py` (state shape, journey stages, bay plan geometry),
`test_trace.py`, `test_agent_runtime.py` (registry schemas incl.
`expose_to_llm`, tool tracing, autonomy gates, brain fallback, granular
change tools, LLM never-executes-mutate, snapshot counts),
`test_intel.py` (tracking, warehouse, flows, plan-change proposals,
brain fallback), `test_exception.py` (customs holds, road-delay slips,
vessel ETD slips, scan purity, traced run).

## 6. How data flows (trace a container end to end)

1. **Seed** — `data/simulator.generate()` builds the world; `store.load_scenario()`
   writes it to SQLite; `record_event("system", "scenario_seeded", ...)`.
2. **Plan** — `mcc_planner.plan()` reads containers/vessels/stowage via
   `call_tool` (traced), derives `MccPlan` + `OutboundContainer` records, and
   saves them via `save_mcc_plans` / `save_outbound_containers` (traced).
3. **State** — `server.py: build_state()` reads the store, derives statuses
   against `sim_now()`, computes KPIs and the PSCH space view, and returns JSON.
4. **Render** — the page's inline JS turns that JSON into the three-column hub.
5. **Live** — every 8 s the page polls `/api/state`; the clock has advanced, so
   statuses/chares/ETAs have moved; when the wave completes the server seeds
   the next one.

## 7. How the agent system operates (the full picture)

The design contract is `AGENTIC_AI_ARCHITECTURE.md`; the living checklist of
integration points is `AGENTIC_AI_INTEGRATION_MAP.md`. Here is how it all fits
**as built today**.

### 7.1 The one pipeline every agent runs through

```
user question / goal / exception scan
        │
        ▼
   POST /api/intel/ask · /api/agent/run · /api/agent/exceptions
        │
        ▼
   AgentRuntime.run(goal, context)          (agents/runtime.py)
        │  AgentBrain (protocol)            (agents/brain.py)
        │    ├─ RuleBasedBrain     (deterministic planner)
        │    ├─ IntelRuleBrain     (Q&A rule brain — PSA Intelligence)
        │    ├─ ExceptionBrain     (exception scan brain — Control Tower)
        │    └─ AgenticAPIBrain    (external LLM, function calling)
        ▼
   agents/tools.py: call_tool(name, args)   (traced — every read/write)
        │
        ▼
   data/store.py (SQLite)  +  execution trace  +  (for changes) approval gate
```

### 7.2 The permission gate (autonomy)

`AgentRuntime.requires_approval()`: `read` always allowed; `mutate` gated in
`advisory` (default); `approval` gated unless `autonomous`. The **LLM brain
additionally never executes any mutate/approval tool inside its loop** — it
returns them as `pending_approval` events; a human clicks **Approve** on the
PSA Intelligence page → `POST /api/agent/approve` → `AgentRuntime.approve()`
executes the tool (traced). This is the human-in-the-loop guarantee.

### 7.3 The LLM seam (currently: local Ollama)

`.env` currently sets:
```
AGENTIC_API_ENDPOINT=http://localhost:11434/v1/chat/completions
AGENTIC_API_KEY=ollama
AGENTIC_API_MODEL=qwen2.5:7b
```
`default_brain()` / `default_intel_brain()` return `AgenticAPIBrain` whenever
`AGENTIC_API_ENDPOINT` is set. Unset it (or stop Ollama) and every page falls
back to the deterministic rule brain instantly — nothing else changes.
`AGENTIC_AUTONOMY` (default `advisory`) is read at runtime start; set
`SIM_SPEED=0` for a frozen, reproducible world.

### 7.4 What a user can do today (the four entry points)

| Entry point | How | What happens |
|---|---|---|
| **PSA Intelligence** (nav) | type "move MAEU4801288 to bin 5-08-1B", "what is the bin utilisation?", "what needs attention?" | Rule brain answers instantly; LLM (when configured) answers with tool calls; plan changes come back as **Approve / Reject** buttons; the conversation is logged to `localStorage` (`psa-intel-thread`) and survives reloads. Multi-action answers render as one labelled **proposal card** per change (`Proposal N · tool` + change + its own Approve/Reject pair), and the model's numbered recap lines are stripped from the prose. |
| **Toolbar goal bar** ("Send a goal to the agent…" + **▶ Run**) | type any goal, e.g. "Plan MCC consolidation for the next 24h" | `POST /api/intel/ask` through the **same shared thread as PSA Intelligence** → Q&A (and any Approve / Reject buttons) rendered in the box below the toolbar; the conversation continues on the PSA Intelligence page; the run lands in the Execution Trace. (`POST /api/agent/run` still exists for direct goal runs.) |
| **Attention needed** (Control Tower) | read the panel or click "Ask the agent" | Live scan of receipt slips / customs holds / loading risk / ETD slips with recommendations; the shortcut pre-fills and sends a question to PSA Intelligence. |
| **Approve / Reject** (PSA Intelligence under a proposed change) | click | `reassign_bin` / `reschedule_receiving_area` / `release_lane` actually applies (or is rejected — trace-only); every page reflects the change on the next 8 s poll. |

### 7.5 Making the plan change (the full loop, verified live)

1. User asks to move a container → the brain (rule or LLM) returns a
   `pending_approval` event (`{kind, tool, args}`) in the answer.
2. The page renders **Approve: reassign_bin / Reject** under the bubble.
3. Approve → `POST /api/agent/approve` → `AgentRuntime.approve()` →
   `call_tool("reassign_bin", args)` → `store.update_mcc_plan` (one row) →
   trace records `approval_required` then `approved`; reasoning line appended.
4. The 8 s poll re-reads the store → the Storage rack grid shows the container
   at its new bin; the thread shows "✓ approved: reassign_bin".
5. Reject → `POST /api/agent/reject` → trace records `rejected`; nothing changes.

## 8. Where to make common changes

| Task | Where |
|---|---|
| Change scenario size / seed / clock speed | `config.py` (`N_CONTAINERS`, `SEED`, `SIM_SPEED`) |
| Change a journey duration | `config.py` (`UNLOAD_MIN`, `DEPOT_DWELL_MIN`, ...) |
| Add a container field | `models/schemas.py` + `data/simulator.py` + `data/store.py` |
| Change the slotting rule | `agents/mcc_planner.py` (`_dwell_level`, `_bin_pool`) |
| Change stacker charge math | `server.py` `_asrs_state()` (drain/refill/threshold) |
| Change lane chips / facility view | `analysis/psch_view.py` (+ `FACILITY_CSS`) |
| Change the classic UI layout/JS | `server.py` `PAGE` string |
| Add a tool for the agent | `agents/tools.py` `register_tool(...)` (set `expose_to_llm=False` for rule-planner-only tools) |
| Change autonomy behaviour | `agents/runtime.py` `requires_approval()` (and `agents/brain.py` for LLM-side gating) |
| Add a question the rule brain can answer | `agents/intel.py` — add an intent branch in `_answer()` |
| Add an exception rule | `agents/exception.py` `scan_exceptions()` |
| Change road-delay behaviour | `agents/mcc_planner.py` `_delay_hours` / `DELAY_RATE` / `DELAY_CHOICES` |
| Change the LLM system prompt / grounding | `agents/brain.py` `AgenticAPIBrain._system_prompt()` |
| Change what the LLM may write | `agents/tools.py` — add/remove granular tools, toggle `expose_to_llm` |
| Change the LLM provider/model | `.env` `AGENTIC_API_ENDPOINT/KEY/MODEL` (currently Ollama + `qwen2.5:7b`) |
| Add a test | `tests/` — follow the existing tmp_path pattern (`_seeded` helper) |
| Wire a new page/endpoint into the classic UI | `server.py` `PAGE` (HTML/JS) + `Handler.do_POST` + `build_state` |

## 9. Conventions and gotchas

- **Never call `datetime.now()` for sim time** — always `config.sim_now()`.
  Tests freeze the clock at `SIM_SPEED=0`.
- **Never compute statuses once and store them** — derive on read against the
  live clock; the terrarium regeneration depends on it.
- **Never write the store directly from a brain** — route through
  `agents.tools.call_tool` so the trace stays complete and the LLM seam works.
- **Keep both UIs working** — `analysis/psch_view.py` + `analysis/kpis.py` +
  `data/facility.build_psch_space` are the shared view layer; changing the
  state shape in `server.py build_state()` should keep the Streamlit app's
  expectations in mind.
- **The page polls every 8 s** — keep `build_state()` fast (it is all reads +
  light derivation; seeding/planning only happen on demand or wave completion).
- **Determinism is a feature** — any demo must be reproducible with
  `SIM_SPEED=0`; don't add unseeded randomness to the generator or planner.
  The road-delay layer is seeded (`SEED:road-delay:{cid}`), so it is
  deterministic too.
- **The sim clock is process-anchored** — `sim_now()` advances from the
  process's boot instant. A fresh script/process sees a *younger* clock than a
  long-running server, so counts (e.g. "containers at PSCH") differ between a
  live page and an ad-hoc verification script. That is the terrarium working,
  not a bug — verify against the running server's numbers.
- **Never trace the 8 s poll** — anything `build_state()` runs must not write
  to the trace (`scan_exceptions` is the pure variant; use `find_exceptions`
  only for user-triggered agent runs).
- **LLM writes are granular by construction** — the bulk plan writers are
  `expose_to_llm=False` and `AgenticAPIBrain` returns every mutate/approval
  tool as `pending_approval`. If you add a plan-writing tool, keep it granular
  and gated; never hand the LLM a whole-batch writer.
- **Windows CRLF** — the repo's files may show CRLF line endings in diffs;
  that is pre-existing, not something to "fix".
- **Run tests with the venv**: `.venv/Scripts/python.exe -m pytest -q`
  (the system Python may lack pytest).

## 10. Quickstart recap

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python.exe server.py            # classic UI → http://127.0.0.1:8513
# or: .venv/Scripts/streamlit run dashboard/app.py
.venv/Scripts/python.exe -m pytest -q         # 92 tests
```

## 11. If the next thread continues from here (recommended first steps)

- Read `AI_INTEGRATION_ROADMAP.md` — **Phase 5** (live-data polish: a
  tool-trace disclosure under each PSA Intelligence answer, README polish,
  demo video) is the only remaining phase.
- The highest-value demo upgrade: let the exception agent's findings drive
  **auto re-proposals** through `/api/agent/run` (detect a delay → propose a
  fix → human approves), closing the detect-to-act loop.
- Before touching timestamps or statuses, re-read §4 (live clock) and §9
  (gotchas). Before touching the agent layer, re-read §7.
