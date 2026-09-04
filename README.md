# PSA Control Tower — Port ↔ PSCH Integration

A simulated control tower that coordinates cargo between **Tuas Container Port** and the **PSA Supply Chain Hub (PSCH)** — the container freight station and distribution centre in Singapore. It demonstrates how an "agentic AI" planning layer could connect a port and a warehouse: every decision (receiving door, rack bin, releasing lane, outbound vessel cell) is made by a planning agent, shown on a live dashboard, and recorded in an auditable trace.

Everything is **synthetic data**: a deterministic, self-consistent simulation of a port + warehouse operation, not live tracking. The default scenario is 400 containers (~340 PSCH-bound across the five hub flows) with ~1,300 pallet shipments.

## The five service flows

Every inbound container is tagged with one flow:

| Flow | What happens |
|---|---|
| **MCC** | Multi-country consolidation: cargo unpacked, grouped by destination, re-packed into outbound containers that must catch a specific ship back at the port |
| **LCL** | Less-than-container-load: cargo delivered by truck to local/regional destinations |
| **FCL** | Full-container-load: the whole container is released by truck, never unpacked |
| **Top Up** | A partially filled container receives additional cargo, then is sealed and released |
| **Transload** | Cargo moved container-to-container without long-term storage |

## Tech stack

- **Python 3.10+** — stdlib-only HTTP server (`server.py`) plus a Streamlit alternative (`dashboard/app.py`)
- **SQLite** (`data/port.db`) — single source of truth shared by both UIs and the agent
- **Pydantic** — domain models (`models/schemas.py`)
- **Pandas** — simulation/KPI data handling
- **Pytest** — test suite

## Quickstart

```bash
# 1. virtual environment + dependencies
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. run the classic control-tower UI (seeds a scenario on first load)
python server.py                 # → http://127.0.0.1:8513

# or the alternative Streamlit UI (same data + agent)
streamlit run dashboard/app.py
```

On first start, `server.py` creates `data/port.db`, seeds a synthetic scenario, and runs the planner agent. The page then auto-refreshes every 8 seconds. Both UIs read the **same** SQLite store and planner agent.

## How it works

- **Seeded, deterministic simulation** — `data/simulator.py` generates the whole world (vessels, bay plans, containers, pallet shipments, the five hub flows) from a random seed. Same seed, same world, every time.
- **Live simulation clock** — timestamps anchor to a fixed `SIM_NOW` and advance at `SIM_SPEED` sim-seconds per real second (default 60×). Every status, ETA and KPI is *derived* from the live clock on each poll, never stored, so the world and the plan can never disagree. Set `SIM_SPEED=0` to freeze the clock for a fully reproducible replay.
- **A terrarium, not a one-shot demo** — when a wave of cargo completes its lifecycle, the server automatically seeds the next wave relative to the live clock and re-runs the planner.
- **Realistic imperfection** — a deterministic road-delay layer (~12% of MCC plans) makes some containers slip past their promised receipt ETA, giving the exception agent genuine events to catch (flagged in the "Attention needed" panel).
- **One source of truth** — all state is written to SQLite; both UIs and all agents read the same store.

### The planning agent

The decision engine is a deterministic, rule-based planner (`agents/mcc_planner.py`) behind a **swappable brain** seam (`agents/brain.py`). For every inbound container it plans:

1. **Journey timeline** — sea arrival → unload → depot → road → PSCH receipt ETA, derived from the vessel's ETA.
2. **Receiving plan** — receiving area + door, based on the inbound arrival rate.
3. **AS/RS putaway** — rack bin (AISLE-LEVEL-BAY) and stacker, applying a slotting rule (fast movers at floor level, slow movers high; reefers → cold room, hazmat → segregated aisle).
4. **Consolidation** — MCC cargo grouped by destination into outbound containers bound for the right vessel, with pallet-pick windows, releasing lanes, and a real loading cell (Bay-Row-Tier) on the vessel.
5. **Status derivation** — outbound containers move `staged → released → in_transit → loaded`; land releases `→ delivered`; top-up jobs `pending → in_progress → done`.

Every decision passes through a declarative **tool registry** (`agents/tools.py`) and is recorded in the execution trace, so the reasoning is inspectable in the UI.

### The agentic AI seam

`agents/brain.py` defines an `AgentBrain` protocol with two implementations:

- `RuleBasedBrain` (default) — deterministic, zero API, reproducible demos.
- `AgenticAPIBrain` — calls any OpenAI-compatible agentic endpoint (OpenAI, Azure, Groq, Ollama, etc.) that supports function calling. Switching brains is a **configuration change, not a code change**.

`agents/runtime.py` provides the run loop, permission gates and tracing. `AGENTIC_AUTONOMY` gates what the agent may do without a human: `advisory` (propose only — default), `semi_autonomous` (low-risk actions auto-applied), `autonomous` (demo mode).

Three agents run on the same runtime + tool registry: the **planner** (`RuleBasedBrain`), the **exception agent** (`agents/exception.py`, watches live state and proposes fixes), and the **Q&A agent** (`agents/intel.py`, powers the PSA Intelligence chat page).

## Configuration

Environment variables (see `config.py`; a `.env` file is read if present, see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `SIM_SEED` | `42` | Random seed → same scenario every run |
| `N_CONTAINERS` | `400` | Total container population |
| `DRAYAGE_TOTAL` | `12` | Trucks available for port ↔ PSCH moves |
| `SIM_SPEED` | `60` | Sim-seconds per real second (`0` = frozen/deterministic) |
| `PSA_PORT` / `PSA_HOST` | `8513` / `0.0.0.0` | Classic UI listen address |
| `PSCH_RECEIVING_LANES` | `10` | Receiving doors |
| `PSCH_AISLES_TOTAL` | `24` | Rack aisles (ambient 1–21, cold room 22–24, aisle 21 hazmat) |
| `PSCH_LEVELS_PER_AISLE` | `12` | Rack height in levels |
| `PSCH_BAYS_PER_LEVEL` | `3` | Bays per level (boxes A/B/C) |
| `PSCH_RELEASING_LANES` | `40` | Dispatch lanes (cycling staging buffer) |
| `PSCH_PALLETS_PER_LANE` | `8` | Staged pallets per releasing lane |
| `MCC_GROUP_SIZE` | `6` | Max inbound containers per outbound consolidation box |
| `UNLOAD_MIN` / `YARD_TRANSFER_MIN` / `DEPOT_DWELL_MIN` / `ROAD_TRANSIT_MIN` | `180`/`45`/`120`/`45` | Port-side stage durations (minutes) |
| `STAGING_MIN` / `MOVE_TO_BIN_MIN` | `20`/`15` | PSCH receiving-side durations (minutes) |
| `AGENTIC_API_ENDPOINT` / `AGENTIC_API_KEY` / `AGENTIC_API_MODEL` | *(empty)* | Switch the agent brain to an external agentic AI API |
| `AGENTIC_AUTONOMY` | `advisory` | `advisory` / `semi_autonomous` / `autonomous` |
| `AGENTIC_MAX_TOOL_ITERATIONS` | `20` | Cap on agent tool-call loop iterations |

## HTTP API

`server.py` serves the dashboard page plus a small JSON API:

| Endpoint | Method | Purpose |
|---|---|---|
| `/` , `/index.html` | GET | The dashboard page |
| `/api/state` | GET | Full JSON view state (inbound/outbound, vessels, berths, facility, KPIs, trace) |
| `/api/health` | GET | `{"ok": true}` liveness probe |
| `/api/bayplan` | POST | Bay-plan HTML for a container (`container_id`) |
| `/map.png` | GET | Static berth-plan aerial graphic |
| `/static/*` | GET | Static assets |
| `/api/seed` | POST | Regenerate the scenario |
| `/api/agent/plan` | POST | Re-run the planner agent |
| `/api/agent/run` | POST | Run the agent runtime on a goal |
| `/api/agent/exceptions` | POST | Run the exception-agent scan |
| `/api/agent/approve` / `/api/agent/reject` | POST | Approve / reject a pending agent proposal |
| `/api/exception/action` | POST | One-click fix from the "Attention needed" panel |
| `/api/intel/ask` | POST | PSA Intelligence Q&A (`question`, `history`) |
| `/api/trace/clear` | POST | Clear the execution trace |
| `/api/berth/inspect` / `/api/psch/inspect` | POST | Log a berth / rack inspection to the trace |

## Headless / CLI usage

```bash
python -m data.seed --seed 42 --containers 400   # generate + load a scenario
python -m agents.run                             # run the planner, print + persist
```

## Project layout

```
config.py                  simulation clock, scenario size, agentic AI settings
models/schemas.py          pydantic models (vessels, containers, plans, ...)
data/simulator.py          seeded synthetic world (fleet, cargo, five hub flows)
data/store.py              SQLite data layer shared by planner + both UIs
data/seed.py               scenario generator (CLI)
data/facility.py           PSCH facility model (rooms, aisles, bins, lanes)
agents/tools.py            declarative tool registry (the agentic AI seam)
agents/brain.py            AgentBrain protocol: rule-based + agentic-API brains
agents/runtime.py          AgentRuntime: run loop, permission gates, tracing
agents/mcc_planner.py      deterministic planning agent (+ road-delay layer)
agents/intel.py            PSA Intelligence rule brain (Q&A + plan proposals)
agents/exception.py        exception agent (live-state scan + fixes)
agents/run.py              headless planner runner (CLI)
analysis/kpis.py           control-tower KPIs
analysis/bayplan.py        vessel bay-plan visualisation (Bay-Row-Tier)
analysis/psch_view.py      PSCH facility view HTML/CSS helpers
server.py                  classic control-tower UI + stdlib JSON API
dashboard/app.py           alternative Streamlit UI
Map.png                    static aerial berth-plan graphic (15 clickable berths)
tests/                     pytest suite
```

## Tests

```bash
python -m pytest
```

Covers the simulator, facility, planner, frontend state, execution trace, agent runtime (tool registry, permission gates, LLM gating), intel Q&A, and the exception agent.

## Notes

- **Synthetic only** — all data is modelled on realistic port/CFS structures; there is no real vessel tracking or GPS.
- **The agent plans; it does not execute** — plans are proposals; nothing physical moves without human approval (in the default `advisory` mode).
- **Deterministic by default** — the rule-based brain makes any demo replayable; the agentic API is a config change away.
- **Two UIs, one world** — the classic UI (`server.py`) is the full hub experience; the Streamlit dashboard shares the store and agent but has a simpler page layout.

## Disclaimer
Some content in this README document is generated with the help of AI.
