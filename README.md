# PSA iWX — MCC Control Tower

Agentic coordination of **multi-country consolidation (MCC)** cargo between the
Tuas Container Port and the **PSA Supply Chain Hub (PSCH)** — the container
freight station that will absorb the consolidation/deconsolidation functions
currently run at Keppel Distripark.

One cargo story, two facilities, one shared view:

1. **Incoming containers** carrying MCC cargo arrive at Tuas — some still at sea
   on inbound vessels, some already discharged and working their way to PSCH.
   They are listed in a **searchable dropdown** (type a container number, vessel,
   status, berth or destination to filter), and picking one slides the map left
   and widens the ship-tracker detail so it fits the screen without resizing.
2. Clicking a container opens a **ship-tracker sidebar**: which vessel is
   carrying it, the voyage ID, how far the ship is from Tuas, how fast it is
   travelling, its ETA at the berth, and a full **bay plan** of the vessel at
   that bay — stacks (rows) 1-16, tiers 02-18 below deck and 82-92 above the
   hatch, drawn as the ship's hull cross-section (the hold tapers to the keel
   like a cone) and showing size / container number / weight, colour-coded by
   destination, with the tracked container's cell highlighted red and
   clickable. Stowage follows a preliminary plan: bays are partly worked — the
   midship block nearly full, shoulders partial, ends sparse — each bay
   carrying its own two-tone port mix (deck band / hold band), with **white
   gaps** above the hatch where deck boxes were already discharged at earlier
   ports. Holds stay full: they carry later-port cargo not yet touched. Every
   stack is filled from the bottom of its band up, so nothing ever floats
   above a gap.
   Heavy boxes sit low, reefers at the power-socket stack, dangerous goods
   segregated, OOG units topmost — so the cell shown is the container's exact
   Bay-Row-Tier position in the moving vessel (20ft in odd bays, 40ft in even
   bays, ~98% of all containers being 40-footers).
3. The **MCC planner agent** derives the full journey from the vessel ETA —
   *En Route (Sea) → Unloaded → Depot → En Route (Road) → Arrived* — including
   the **ETA at the PSCH doorstep**.
4. Before the cargo even arrives, the agent plans PSCH for it: it opens
   **receiving areas** according to the inbound volume rate and assigns the
   **robot putaway bins** for the palletised cargo inside.
5. The agent then groups the inbound containers by final destination into
   **outbound consolidation containers**, times the **pallet pick** against the
   stuffing window (itself driven by when the bound vessel will arrive at the
   port to load), and releases **loading lanes** per outbound container number.
6. Once loaded, the outbound container shows **"Loaded"** with the vessel it is
   bound for — its **berth rectangle pops up on the map** — plus when the vessel
   leaves the port, the **exact loading cell on the vessel**, and the **ETA to
   arrive at the quay loading area**.
7. A dedicated **PSCH Space** page visualises the facility itself: AMBIENT and
   COLD ROOM blocks laid out Receiving → Storage → Dispatch with a one-way
   flow (like a cold-storage warehouse), every rack (aisle) selectable down to
   its **AISLE-LEVEL-BAY bin grid** (conventional distribution-centre naming,
   e.g. 1-12-2A = Aisle 1, Level 12, Bay 2A — the box letter is written
   directly on the bay number), the **10 receiving staging lanes** (numbered
   plainly 1..10, listing the containers unloaded in each lane) at the inbound
   area, the **26 physical releasing lanes** (numbered 1..26, parked side by
   side as narrow vertical blocks like dock doors along a warehouse wall)
   above the facility — the agent allocates each consolidation container one
   lane, or a contiguous group of adjacent lanes, to stage the pallets waiting
   to be loaded into that same container, and clicking a container shows the
   cargo planned to stage on its lanes — and
   each planned bin colour-coded by the container's journey status — bins are
   *reserved* by the agent before the cargo arrives and turn green when the
   container is in the bin. Aisles are numbered 1-24 (ambient 1-21, cold room
   22-24, aisle 21 segregated for dangerous goods); every aisle has 12 levels
   and each level has 3 bays with boxes A/B/C. A **slotting rule** optimises
   the storage height: cargo released soon is put at floor level, slower
   movers higher. Reefer cargo
   routes to the cold room. Selecting a container from the list, a rack or a
   bin highlights its planned bin and opens the ship-tracker detail.

The agent is a deterministic rule-based planner (zero API, zero cost) so every
demo is fully reproducible; all its state reads and decisions are recorded in an
auditable **execution trace**.

## Quickstart

```bash
# 1. (recommended) create a virtualenv, then install
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. run the dashboard (auto-seeds a scenario and runs the planner on first load)
python server.py                  # classic early-2000s UI, http://127.0.0.1:8513
# or the Streamlit UI:
streamlit run dashboard/app.py
```

### Facility map + berth plan

The MCC Tracker shows a **static aerial graphic** of the Tuas berth plan
(`Map.png` at the project root — swap the file to change the artwork). It is
deliberately **not** a GPS tracker: containers and trucks carry no positioning.
Each of the fifteen berths (B1–B15, matching the berth markers drawn on the
graphic) is a clickable rectangle — the berth of the vessel each outbound
container is bound for **pops up highlighted** when that container is selected. Occupancy comes from the simulated `vessels`
table, so it stays in sync with the rest of the scenario. Berth rectangles are
defined in `server.py` (`BERTHS`, as % of the image).

Two frontends are available: `server.py` serves the classic, table-based,
early-2000s control-tower UI (no extra deps, stdlib HTTP server), and
`dashboard/app.py` is the Streamlit UI. Both sit on the same SQLite store and
planner agent.

## Headless usage

```bash
python -m data.seed --seed 42 --containers 60   # generate + load a scenario
python -m agents.run                            # run the MCC planner, print + persist
```

## Tests

```bash
python -m pytest
```

## Architecture

```
config.py                      sim clock, scenario size, MCC planning constants
models/schemas.py              vessels w/ tracking, containers w/ stow cells,
                               MccPlan, OutboundContainer (pydantic)
data/simulator.py              seeded synthetic world (fleet, cargo journey, pallets)
data/store.py                  SQLite data layer shared by planner + dashboard
data/seed.py                   generate + load a scenario (CLI)
agents/mcc_planner.py          the agent: journey timeline, receiving plan,
                               bin allocation, consolidation schedule, trace
agents/run.py                  headless planner runner
analysis/kpis.py               MCC pipeline KPIs
server.py                      classic 2000s HTML dashboard + stdlib JSON API
dashboard/app.py               alternative Streamlit dashboard
```

## Notes / decisions

- **Time base:** synthetic timestamps anchor to `config.SIM_NOW`
  (`2026-08-16T12:00Z`), not the wall clock, for reproducibility.
- **Journey statuses** are derived from the agent's plan times, so the world
  and the plan can never disagree; the seeded scenario deliberately spans all
  five stages at once for the demo.
- **Agent is deterministic**: it reads state through a tool layer (each call
  logged to the trace), derives every stage time from the vessel ETA, and
  writes only plans — no execution.
- **Re-running the planner** replaces the current plan batch; **Regenerate**
  builds a fresh scenario and the planner runs automatically on first view.
- All data is **synthetic**, modelled on realistic port/CFS structures — no
  real CITOS/PORTNET access.
