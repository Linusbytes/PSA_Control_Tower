# PSA Control Tower — Port ↔ PSCH Integration

A simulated control tower that coordinates cargo between **Tuas Container Port**
and the **PSA Supply Chain Hub (PSCH)** — the container freight station and
distribution centre in Singapore that will absorb the consolidation /
deconsolidation work currently done at Keppel Distripark.

The software tells one continuous story: cargo arrives at the port inside
shipping containers, travels to PSCH to be unloaded, stored and re-packaged,
and then leaves PSCH again — either back to the port to be loaded onto an
outgoing ship, or by truck to destinations across Singapore and the region.
Every decision in that journey — which door to receive a container at, which
rack bin to store its cargo in, which lane to stage an outgoing load, which
ship and loading cell to send it to — is made by a built-in **planning agent**
and shown on a live dashboard, with every step recorded in an auditable log.

Everything you see is **synthetic data**: a deterministic, self-consistent
simulation of a real port + warehouse operation, not live tracking from the
actual port. It is designed as a demo/prototype of how an "agentic AI"
coordination layer could connect a port and a warehouse, and as a visualisation
of the domain itself.

> **Companion guide:** [`SOFTWARE_GUIDE.md`](SOFTWARE_GUIDE.md) is the full
> self-contained reference — the complete functionality suite, a change log of
> everything built up to now, a dedicated chapter on **how the AI works**, and
> an honest list of limitations with workarounds.

---

## 1. Before you start: containers and ports in plain language

If you have never worked with shipping containers, here is everything you need
to understand the rest of this document.

**A shipping container** is a big standardised steel box used to move cargo on
ships, trucks and trains. Standard lengths are **20 feet** ("20ft") and **40
feet** ("40ft"); a **40HC** (high cube) is a 40ft box that is a bit taller,
giving more volume for light cargo. Containers have an official number printed
on the door — e.g. `MSCU1234567` — made of a 4-letter owner code followed by 7
digits (the ISO 6346 standard). Roughly 98% of the containers in this
simulation are 40ft-family boxes.

**A port** (here, Tuas Container Port) is where ships park and containers move
between ships, trucks and the port's yard. Key port concepts:

- **Berth** — a marked parking spot alongside the quay wall where a ship ties
  up. This simulation's terminal has 15 berths (B1–B15), drawn on an aerial
  map.
- **Vessel / voyage** — a ship (e.g. *MAERSK EGYPT*) and its specific sailing
  ("voyage ID", e.g. `MAERSK-EG-24E`).
- **ETA / ETD** — *estimated time of arrival* / *estimated time of departure*
  of the vessel at the port.
- **Discharge** — lifting containers off the ship with a quay crane.
- **Depot yard** — the port's open storage area where discharged containers
  wait to be collected by a truck.
- **Drayage** — the trucking of containers between the port and nearby
  facilities.
- **Prime mover** — the truck (tractor unit) that pulls a container chassis.

**A container freight station (CFS) / distribution centre (DC)** — in this
story, **PSCH** — is a warehouse that does more than store boxes. It receives
containers, unpacks them, stores the cargo on racks, and re-packs cargo into
other containers or trucks. This is the industry's **"deconsolidation /
consolidation"** service:

- **Deconsolidation** — unpacking a container and breaking its cargo into
  smaller units (pallets of goods).
- **Consolidation** — gathering cargo from several containers and packing it
  together into one outgoing container or one truckload, usually because they
  all share the same final destination.

**The four ways cargo moves through a hub** (the industry's "service flows"):

| Flow | Full name | What it means in plain language |
|---|---|---|
| **MCC** | Multi-country consolidation | Cargo from many shippers/countries is unpacked, grouped by destination, and re-packed into outgoing containers that must catch a specific ship. This is the classic CFS business. |
| **LCL** | Less than Container Load | A container filled with cargo from several different shippers ("less than a full load" each). It is unpacked at PSCH and the cargo is then delivered by **truck** to different destinations (local Singapore areas or regional cities). |
| **FCL** | Full Container Load | A container belonging to one shipper with a full load. PSCH does not unpack it — the whole container is received, staged briefly, and **released by truck** to its destination. |
| **Top Up** | Re-consolidation / topping up | A container arrives only partially full. PSCH consolidates **additional cargo into it**, then seals and releases it. The forwarder may ask for this at their discretion. |
| **Transload** | Container-to-container transfer | Cargo is moved from one container into another container (e.g. a 40ft split into two 20ft loads, or hub-to-hub transfers) without long-term storage. |

**Where cargo sits inside PSCH:**

- **Rack / aisle** — the tall steel shelving rows of the warehouse. PSCH has 24
  aisles: **aisles 1–21** are the **AMBIENT** room (normal dry cargo; aisle 21
  is segregated for dangerous goods) and **aisles 22–24** are the **COLD ROOM**
  (reefer cargo, chilled/frozen).
- **Bin** — one single-pallet storage location in a rack. Bins are named the
  distribution-centre way, **AISLE-LEVEL-BAY**: `1-12-2A` = *Aisle 1, Level 12,
  Bay 2A*. Every aisle has 12 levels (heights) × 3 bays × 3 box positions
  (A/B/C) = **108 bins per aisle**.
- **Receiving lanes** — the numbered doors (1–10; the UI shows just the lane
  number, not an "RA-" code) where inbound containers are unloaded.
- **Releasing lanes** — the 40 numbered dock positions along the dispatch wall
  where outgoing cargo is staged (parked on pallets) waiting to be loaded into
  a truck or a container. One outgoing load can occupy one lane, or several
  adjacent lanes if it has many pallets. The lanes are a **cycling staging
  buffer**: a lane is freed once its box is stuffed, so successive
  consolidation boxes reuse it.

**Special cargo terms:**

- **Reefer** — refrigerated container/cargo (kept cold). Reefers need power
  sockets, so on ships they are stowed at the power-socket stacks and in PSCH
  they go to the cold room.
- **DG / Hazmat** — dangerous goods; kept segregated (outermost ship stack,
  and PSCH's dedicated aisle 21).
- **OOG** — out of gauge (oversized cargo that cannot fit a normal container);
  stowed on top where it is loaded last and discharged first.
- **Customs status** — cleared / pending / held by customs.
- **Stowage / bay plan** — how containers are arranged inside a ship. Ships are
  organised in **bays** (columns along the hull, numbered from the bow), and
  each bay has **stacks/rows** (1–16 across the ship) and **tiers** (levels:
  02–18 are below the deck in the hold, 82–92 above the deck). A container's
  exact position is written **Bay-Row-Tier**, e.g. `Bay 33(34) · Row 08 · Tier
  86`. 20ft boxes sit in odd-numbered bays, 40ft boxes in even-numbered bays (a
  40ft bay "spans" the odd bay before it, written `33(34)`).

---

## 2. The story the software simulates

One continuous cargo story runs through the whole application. It repeats for
every inbound container, 24 hours a day:

**Stage A — At sea.** A vessel is inbound to Tuas, tracked live (nautical miles
out, speed in knots). It carries containers stowed in its bay plan; the
containers destined for PSCH are marked in the plan.

**Stage B — Port side.** The vessel arrives at its berth (ETA) and the
containers are discharged (unloaded) by quay cranes. They are transferred to
the port depot yard, dwell briefly, then dispatched by truck (drayage) to PSCH
— a short ride, since PSCH is adjacent to the port.

**Stage C — PSCH doorstep.** The container arrives at PSCH (the agent's planned
"receipt ETA"). It is received at one of the receiving areas/doors, unloaded,
and its pallets are moved by **stacker robots** to a planned rack bin.

**Stage D — Storage.** The pallets dwell in their bins. A **slotting rule**
decides the height: cargo that will be released soon is stored at floor level
for fast stacker retrieval; slower movers go higher up the rack.

**Stage E — Outbound.** Depending on the container's **flow** (section 3), the
cargo is picked from its bins and either:

- stuffed into an **outbound consolidation container** that returns to the port
  and must be loaded onto a specific vessel before it sails (MCC), or
- built into an **LCL delivery unit** — cargo grouped by destination and sent by
  truck (local Singapore or regional), or
- released as a **whole FCL container** by truck, or
- consolidated into a **top-up container**, sealed and released, or
- **transloaded** container-to-container.

Every stage time is derived backwards from the vessel's ETA/ETD, so the world,
the plan and the KPIs can never disagree.

---

## 3. The PSCH hub and its five service flows

Every inbound container in the simulation is tagged with **one** of the five
service flows above. The default world (400 containers, seed 42) contains about
128 PSCH-bound containers in this mix, plus plain import/transshipment boxes
that only dwell in the port yard:

| Flow | Share | Where you see it in the app |
|---|---|---|
| MCC | ~36% | Inbound list → Storage putaway → Outbound ▸ MCC |
| LCL | ~26% | Inbound list → Storage putaway → Outbound ▸ Distribution (LCL delivery units) |
| FCL | ~16% | Inbound list → Outbound ▸ Distribution (FCL land releases) |

> **Display categories.** In the UI the five internal flows are shown as four
> categories — **MCC · Distribution · Top Up · Transload** — because LCL and
> FCL containers are both *Distribution* (they leave PSCH by land). The
> inbound filter chips and KPI counts use these four; the Distribution page
> still distinguishes LCL delivery units from FCL land releases.
| Top Up | ~14% | Inbound list → Outbound ▸ Top Up |
| Transload | ~8% | Inbound list (transfer bays in Storage) |

Because PSCH runs **24/7 with a high degree of automation and robotics**, the
simulation deliberately has **no shift structure or breaks**: vessels arrive
and sail at all hours, containers arrive continuously across the whole day, and
the numbers are always flowing.

---

## 4. Running the software

### Requirements

- Python 3.10+
- Dependencies (`requirements.txt`): `streamlit`, `pydantic`, `python-dotenv`,
  `pandas`, `pytest`.

### Quickstart

```bash
# 1. (recommended) create a virtual environment, then install
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. run the classic control-tower UI (auto-seeds a scenario on first load)
python server.py                   # → http://127.0.0.1:8513

# or the alternative Streamlit UI:
streamlit run dashboard/app.py
```

On first start, `server.py` creates the SQLite database
(`data/port.db`), seeds a synthetic scenario, and runs the planner agent
automatically. The page then **refreshes itself every 8 seconds** to show the
live state.

Two frontends exist, both reading the **same** SQLite store and planner agent:

- **`server.py`** — the classic, early-2000s "control-tower" UI (grey/navy
  bevel styling, deliberately retro: *"Best viewed at 1024×768 in Netscape
  4.0+"*). It is the full hub interface described in section 5 and runs on the
  Python standard library only.
- **`dashboard/app.py`** — a Streamlit alternative with tabs (Track / Plan /
  Space / Tower / Trace). It shares the same data and agent but has not been
  restructured into the hub's page layout.

### Toolbar buttons

The buttons sit as one compact group at the top-left of every page:

- **⟳ Regenerate** — wipes the database and builds a brand-new scenario
  (same seed → same world), then re-runs the planner and refreshes the page.
  It is the same "start a fresh wave" action the terrarium does automatically
  when a lifecycle completes.
- **▶ Run Hub Planner** — re-runs the planning agent over the **current**
  world: journey timelines, receiving areas, putaway bins, consolidation
  groups, releasing lanes and outbound containers. Deterministic — same
  world gives the same plan.
- **⛶ Full Screen / ✕ Exit Full Screen** — toggles the in-app fullscreen
  mode (hides the title bar and tightens the ticker so the dashboard uses the
  full window height). Exactly two symbols, one per state: ⛶ when normal,
  ✕ when fullscreen. It is an in-app "hide the chrome" mode, not the
  browser's native F11 fullscreen.
- **⚙ Flash** — opens the **flash display settings** (the Bloomberg `PDF
  <GO>` equivalent): live value flashes can be switched off entirely, and
  their speed, brightness and colour palette tuned (see §5.9).
- **PLANNER badge** — shows which brain is active (rule-based by default).
- **SIM CLOCK** — the simulated current time, live: the world advances at
  `SIM_SPEED` sim-seconds per real second (default 60×, so one simulated hour
  passes every real minute; set `SIM_SPEED=0` to freeze it for a fully
  reproducible replay).

---

## 5. Tour of the pages

The top navigation bar has three top-level items: **PSCH ▾** (a dropdown with
the five hub pages), **Control Tower**, and **Execution Trace**.

```
PSCH ▾
├─ Inbound            every container entering PSCH, all five flows
├─ Storage            receiving, AS/RS stacker putaway & picking — the facility view
├─ Outbound ▸
│   ├─ MCC            back-to-port consolidation containers (vessel-bound)
│   ├─ Distribution   LCL deliveries + FCL land releases
│   └─ Top Up         container re-consolidation (topping up)
└─ Quality Control    cargo survey · sampling · repack · rework
```

Each hub page follows the same three-column arrangement: **a list on the left**,
**a visual board in the centre** (where the berth map or floorplan lives), and
**a detail inspector on the right**.

### 5.1 Inbound — every container entering PSCH

The master list of *all* PSCH-bound containers, regardless of flow (MCC, LCL,
FCL, Top Up, Transload). The KPI strip across the top shows totals per flow,
how many are still at sea, how many have arrived at PSCH, the arrival rate over
the next 6 hours, the average sea→PSCH pipeline duration, and bin utilisation.

- **Filter chips** (All / MCC / Distribution / Top Up / Transload) narrow the
  list — clicking a chip opens the dropdown showing exactly the matching
  containers (e.g. Distribution = the 53 LCL + FCL land-bound boxes); the
  **search box** filters as you type (container number, vessel, status, berth,
  destination…).
- Selecting a container opens its **ship-tracker detail** on the right: the
  carrying vessel, voyage, distance from Tuas and speed if still at sea, the
  full journey timeline (sea arrival → unload → depot → road → PSCH receipt
  ETA), its receiving area and door,  its planned bin and AS/RS stacker, its
  pallet-pick time, release lane and consolidation group, and the agent's
  reasoning. The columns **reflow** (the list narrows, the map and inspector
  widen) so the detail fits — and everything resets when you leave the page and
  come back.
- The **centre board** is the **Tuas berth plan**: a static aerial graphic
  (`Map.png`) with 15 clickable berth rectangles (B1–B15). Berth occupancy
  comes from the simulated vessels — a docked vessel sits on its berth, an
  inbound vessel's planned berth is shown, free berths are marked available.
  This is deliberately **not a GPS tracker**: containers and trucks carry no
  positioning.
- Each container also has a **bay plan** — the exact cross-section of the
  carrying vessel at the container's bay, drawn as the ship's hull (the hold
  tapers to the keel). Stacks run 1–16, tiers 02–18 below deck and 82–92 above
  deck (the hatch sits between), and every cell shows size/container
  number/weight, colour-coded by destination, with the tracked container's cell
  highlighted. The stowage follows port conventions: heavy boxes low, reefers
  at the power-socket stack, dangerous goods segregated, OOG on top, 20ft in
  odd bays and 40ft in even bays.

### 5.2 Storage — the agent's receiving / putaway / picking playground

This is the physical PSCH facility, organised for the automation story: which
inbound container is being unloaded at which door, and exactly which rack bin
its cargo is put away to.

- **Left list — "Inbound Cargo by Container ID"**: each inbound container
  with how many cargoes (pallet shipments) are being unloaded from it, its
  receiving area, and the bin its cargo is planned into. The hint "Select a
  container to open its receiving & putaway plan" sits right below the header,
  with the sub-description "Includes Bin Location and Stacker responsible"
  underneath it. Selecting one opens its receiving & putaway plan in the
  right-hand **Container Plan Detail** inspector, with the agent's reasoning
  below it. Clicking the selected container again — or clicking empty space
  anywhere on the page — deselects it and restores the original three-column
  arrangement.
- **Centre board** — the facility, drawn top to bottom:
  - the **site strip** (`PSA SUPPLY CHAIN HUB — TUAS · Tuas South Ave 5 ·
    Gate 1 · Gate 2`),
  - the **yard strip** (truck marshalling · container staging),
  - the **flow strip** — `INBOUND ⟶ RECEIVING ⟶ PUTAWAY ⟶ STORAGE ⟶ PICK ⟶
    RELEASING ⟶ OUTBOUND` — the one-way goods flow the whole layout obeys,
    rendered as seven numbered steps in **one uniform colour** (navy, with
    inverted white step-number chips) so the strip reads as a single pipeline
    at a glance,
  - the **AS/RS strip** — the automated storage & retrieval system: all 8
    stackers with live charge %, which are on the charging station (under 45%
    charge), and the charging bays — tracked as a PSCH metric in the KPI strip
    and Control Tower,
  - the **10 receiving lanes** — numbered 1–10 only (no "RA-" prefix), drawn
    as horizontal blocks in the same visual language as the releasing lanes;
    each block shows the lane number and the container numbers being unloaded
    (or staged) there, one per line and **never wrapped** — the row scrolls
    horizontally if it is wider than the pane,
  - the **24 racks (aisles)** of the two rooms — AMBIENT aisles 1–21 (aisle 21
    hazmat-segregated) and COLD ROOM aisles 22–24 — each selectable,
  - the **40 releasing lanes** along the dispatch wall (the row scrolls
    horizontally), each assigned to the outbound group staging there (or
    free). The lanes are a **cycling staging buffer**: a lane may serve
    successive consolidation boxes whose stuffing windows don't overlap,
    exactly like a real dispatch area whose bays are freed once a box is
    sealed and trucked to the quay.
- Clicking an aisle opens the **bin grid** below: every bin of that aisle
  (AISLE-LEVEL-BAY names) colour-coded by the container's journey status —
  **yellow = reserved** (agent planned it before the cargo arrived), **green =
  arrived** (container at PSCH), **grey = empty**. Whole-container flows (FCL /
  Top Up / Transload) are staged in yard slots and bays, not rack bins, so they
  never occupy the grid.
- The racks are **never empty**: a deterministic **prior-wave dwell-stock
  floor** (`data/facility.py`) tops every aisle up into a realistic 35–60%
  utilisation band (a few quiet aisles ~10%, the cold room and the segregated
  DG aisle in their own bands), so the facility looks like a real working CFS
  while the current wave's containers churn on top of it. Stock bins show as
  green "DWL-…" dwell pallets (click one to inspect it) and are never
  double-booked with a wave bin.
- The **slotting rule** is visible in the plans: cargo whose pallets are picked
  soon is stored at floor level; slower movers are stored higher, so the
  AS/RS stackers reach the soon-to-ship cargo without climbing.

### 5.3 Outbound ▸ MCC — the back-to-port consolidation tracker

The vessel-bound side of the MCC story, restored as its own page: cargo
pallet-picked and stuffed into a 40ft outbound consolidation container that
returns to Tuas to be loaded onto a specific vessel before it sails.

- **Left list** — every consolidation container with its status (`staged →
  released → in_transit → loaded`), destination, bound vessel, vessel ETD and
  ETA at the quay loading area.
- **Centre board** — the Tuas berth plan map. Selecting a container highlights
  the berth of the vessel it is bound for (it pops up on the map), and clicking
  any berth rectangle inspects the docked/inbound/available vessel there.
- **Right inspector** — the full marine detail: bound vessel and voyage, its
  berth, ETD, loading cutoff, the exact **loading cell** (Bay-Row-Tier) the
  container will occupy on the vessel (drawn as a bay plan), the stuffing /
  pallet-pick window, the released loading lane, the PSCH releasing lanes
  staging its pallets, road-departure time, ETA at the quay loading area, and
  the agent's reasoning.

### 5.4 Outbound ▸ Distribution — local delivery and land release

One page, two release kinds (filter chips, not sub-menus):

- **LCL delivery** — cargo from several LCL containers is grouped per
  destination into one delivery unit. The board shows destination blocks with
  which source containers feed each unit, how many pallets, and the build /
  release / road-departure / ETA-at-destination timeline.
- **FCL land release** — whole containers released by truck to their
  destination (local Singapore ~2h away, regional destinations ~5h away).

Each release shows its status (`staged → released → in_transit → delivered`).
The vessel-bound (back-to-port) containers that the earlier design grouped here
now live on the **MCC** page (section 5.3).

### 5.5 Outbound ▸ Top Up — container re-consolidation

A separate planned workflow (already pre-wired for the future AI agent): which
containers are being **topped up** (partially filled containers receiving
additional cargo), which are done, and the metrics around each job.

- The list shows every top-up job with its destination, status
  (`pending → in_progress → done`), pallets added, work window, seal time and
  release ETA.
- The board shows the **re-consolidation workflow strip** — `RECEIVE ⟶ STAGING
  ⟶ BAY ⟶ SEAL ⟶ RELEASE` — and the **10 top-up bays**, each showing the
  container staged in it and its job status.

### 5.6 Quality Control — survey · sampling · repack · rework

QC tasks are derived from the cargo profile (hazmat cargo gets surveyed or
sampled, oversized cargo repacked, reefers sampled, customs-held cargo
surveyed, plus a rotation of sampling/rework jobs). Four kinds of tasks run
across four stations (QC Bay 1–4), each with its window, flow, destination and
a scope note explaining the operation. Statuses: `pending → in_progress →
done`.

### 5.7 Control Tower

A consolidated KPI page over the whole hub: the pipeline totals (inbound
containers, at sea/on road, arrived at PSCH, average pipeline, arrival rate,
bin utilisation), outbound-loaded counts, land releases, top-up jobs, QC tasks,
yard and drayage utilisation, plus three breakdown tables: inbound journey
stages, outbound consolidation status, and the PSCH hub flows (distribution /
top-up / QC by status).

### 5.8 Execution Trace

The audit log: every event the system and the agent record — `Time · Actor ·
Event · Detail`. Actors include the planner agent (e.g. `mcc_plan_computed`,
`consolidation_group_planned`, `slotting_applied`), the system (`scenario_seeded`)
and operators (`berth_inspected`, `psch_rack_inspected`). Inspecting a berth or
a rack from the UI logs a trace event. Use **Clear Trace** to reset it.

### One story, three views

The same outbound cargo appears on the **Inbound** page (in the master list,
with its consolidation group), on **Storage** (pallets picked from bins and
staged on releasing lanes), and on **Outbound ▸ MCC** (the container on the
road or at the port, bound for its vessel). Pick any page and the numbers
agree, because they all read the same plans from the same store.

### 5.9 Live value flashes — Bloomberg-style alerts

Every number, container row, stacker, lane and bin cell that changes between
polls **flashes the instant the new value is rendered**, exactly like a
Bloomberg terminal signalling an incoming tick:

- **Directional colour coding** — green flash + **▲** when the value went up,
  red flash + **▼** when it went down, amber when it changed without a clear
  direction (a container's journey status flipping, a bin becoming occupied,
  a lane gaining/losing cargo). The flash hits the *field that changed* — the
  KPI cell, the container row, the stacker charge, the lane block, the bin
  cell — not the whole page.
- **What flashes**: KPI strips (Inbound, Storage, MCC, Distribution, Top Up,
  QC, Control Tower), container rows when their status changes, AS/RS stacker
  charge % (green as it recharges, red as it drains), receiving/releasing
  lanes when cargo arrives or leaves, bin-grid cells when a bin transitions
  reserved → occupied, and the Tower's breakdown tables.
- **Settings (the `PDF <GO>` equivalent)** — the **⚙ Flash** toolbar button
  opens a small panel that customises the alerts and persists the choice in
  the browser (`localStorage`), per Bloomberg's own guidance:
  - **On/off** — "Live value flashes" switch: turn flashing off entirely for
    a low-distraction session (values keep updating silently; the switch
    remembers so no huge diff flashes when you re-enable).
  - **Speed** — Slow (4s) / Normal (2.5s) / Fast (1.1s) fade.
  - **Brightness** — Subtle / Normal / Vivid (controls the flash intensity
    and glow radius).
  - **Palette** — Classic (green/red/amber), **Colour-blind safe**
    (blue/orange/yellow) and Mono (blue/grey) for accessibility.
- The flash animations are driven by CSS variables, so the customisation is
  purely presentational — the underlying values and the 8s poll never change.

---

## 6. How the simulation works

- **Synthetic, seeded, deterministic.** `data/simulator.py` generates the whole
  world (vessels, bay plans, containers, bookings, pallet shipments, yard,
  drayage) from a random seed — same seed, same world, every time (default
  `SIM_SEED=42`).
- **Live simulation clock.** All timestamps anchor to `SIM_NOW`
  (`2026-08-16T12:00Z`) but the clock **advances** at `SIM_SPEED` sim-seconds
  per real second (default 60×). Every status, ETA and metric is re-derived
  from `sim_now()` on each 8-second poll, so the world moves by itself — no
  "enable live mode" switch, nothing to start. Set `SIM_SPEED=0` to freeze
  the clock for a fully reproducible replay.
- **One source of truth.** Everything is written to SQLite (`data/port.db`);
  both UIs and the planner read the same store, so pages always agree.
- **Statuses are derived, never stored.** A container's journey stage
  (`En Route (Sea) → Unloaded → Depot → En Route (Road) → Arrived`), the AS/RS
  stacker charge % and charging rotation, outbound `staged → released →
  in_transit → loaded`, and every KPI are computed from the agent's plan times
  against the **live** sim clock, so the world and the plan can never disagree
  and the numbers genuinely change while you watch. The seeded world
  deliberately spans all stages at once so every page shows a live, mixed
  picture.
- **Real-world imperfection, on purpose.** A deterministic **road-delay
  layer** (~12% of MCC plans, seeded 1–8 h) makes a subset of containers slip
  past their *promised* receipt ETA. This is what gives the **exception
  agent** genuine events to catch — the dashboard shows the promise and the
  slip, and the Control Tower's Attention needed panel flags them with
  recommendations.
- **A terrarium, not a one-shot demo.** When the current wave has run its full
  lifecycle (every inbound container's pallets picked and every outbound
  container loaded back to the port or released by land), the server
  **automatically seeds the next wave** relative to the live clock and re-runs
  the planner — the ecosystem keeps living indefinitely. The `⟳ Regenerate`
  button still forces a fresh wave by hand.
- **24/7 operation.** Vessel ETAs/ETDs and container arrivals are spread across
  the whole day (including small hours) — no shift structure, matching the
  brief that PSCH runs around the clock with a large degree of automation.
  Each container's road dispatch carries a per-container jitter (customs
  holds, truck availability), so receipts at the PSCH doorstep flow
  CONTINUOUSLY — average spacing ~7 sim-minutes, worst-case quiet gap ~2.6
  sim-hours — instead of bunching into tight vessel-ETA clusters with long
  dry spells. Every KPI is derived from the same live sim clock as the
  facility view, so "Arrived @ PSCH", "At sea" and the storage occupancy
  move together on every poll.
- **Realistic geometry.** Every PSCH container is taken from a real cell in its
  vessel's bay plan (correct Bay-Row-Tier, bay parity, special-cargo placement),
  so ship-tracker, berth highlights and stowage all agree on the same cell.
- **Data density + performance.** The default world is 400 containers (~340
  PSCH-bound across the five flows, ~1,300 pallet shipments). Putaway bins are
  interleaved across **every** aisle of the zone (shuffled per rack level, not
  filled aisle-by-aisle), a vessel call is consolidated into **several**
  outbound boxes (chunks of ≤ 6 source containers), and the 40 releasing lanes
  are reused time-aware. The 8-second state poll stays lean: vessel bay plans
  are served **on demand** (`/api/bayplan`, cached client-side) instead of
  being embedded into every container row, aisle geometry is cached once, and
  `build_state` returns in ~40 ms.

---

## 7. The planning agent

The **decision engine** of the software. It is deterministic and rule-based
(zero API, zero cost) so demos are fully reproducible, but it is built behind a
**swappable brain** seam so an external agentic AI API can be plugged in later.

What the agent does for every inbound container:

1. **Journey timeline** — derives sea arrival → unload (≈180 min) → depot
   transfer (≈45 min) → depot dwell (≈60–120 min) → road to PSCH (≈45 min) →
   receipt ETA, all from the carrying vessel's ETA.
2. **Receiving plan** — opens receiving areas according to the inbound arrival
   rate (containers/hour) and assigns each container a receiving area + door.
3. **AS/RS stacker putaway** — assigns the rack bin (AISLE-LEVEL-BAY) and the
   AS/RS **stacker** that moves the pallet, applying the **slotting rule**
   (dwell-based height: fast movers at floor level, slow movers high). Reefer
   cargo → cold room; hazmat → the segregated aisle 21; the rest → ambient.
   Bins are *reserved* before the cargo arrives and turn *arrived* when the
   container reaches PSCH. The 8 stackers run on a staggered 24/7 charge
   rotation — charge % declines ~8%/hour with use, a stacker parks itself at
   the charging station when it drops to 45%, and tops back up (~30%/hour)
   before resuming. The rotation is driven by the live sim clock, so the
   charges move on every poll and are tracked as a metric in the KPI strip and
   Control Tower.
4. **Consolidation** — groups MCC cargo by final destination into outbound
   containers bound for the vessel serving that destination, times pallet
   picking against the stuffing window (itself driven by the vessel's ETD and
   the loading cutoff), releases loading lanes, allocates the physical
   **releasing lanes** (contiguous spans, one lane per up-to-8 pallets), and
   assigns each outbound container a real loading cell on its vessel.
   LCL cargo is grouped into delivery units by land destination; FCL / Top Up /
   Transload containers are planned whole (staging slots, gates, bays).
5. **Status derivation** — outbound containers move `staged → released →
   in_transit → loaded`; land releases `staged → released → in_transit →
   delivered`; top-up jobs `pending → in_progress → done`; QC tasks likewise.

**Auditability.** Every state read and every decision passes through a
declarative **tool registry** (`agents/tools.py`) and is recorded to the
execution trace, so the reasoning is inspectable in the UI.

**The agentic AI seam.** `agents/brain.py` defines an `AgentBrain` protocol with
two implementations: the deterministic `RuleBasedBrain` (default) and an
`AgenticAPIBrain` that calls any OpenAI-compatible agentic endpoint.
`agents/runtime.py` provides the run loop, permission gates and tracing.
`AGENTIC_AUTONOMY` controls how much the agent may do without a human:
`advisory` (propose only — default), `semi_autonomous` (low-risk actions
auto-applied), or `autonomous` (demo mode). See `AGENTIC_AI_ARCHITECTURE.md`
and `AGENTIC_AI_INTEGRATION_MAP.md` for the full design.

---

## 8. Agentic AI integration & deployment guide

### Is it easy? — Yes, by design

The whole stack was built so the LLM brain is a **configuration change, not a
code change** (§7). The tool registry, runtime loop, permission gates and
trace are already implemented and tested (`tests/test_agent_runtime.py`).
You do **not** need to touch the dashboard, the data layer, or the planner.

### Step 0 — the five-minute smoke test

1. `cp .env.example .env` and set:
   ```
   AGENTIC_API_ENDPOINT=https://api.openai.com/v1/chat/completions
   AGENTIC_API_KEY=sk-...
   AGENTIC_API_MODEL=gpt-4o-mini
   AGENTIC_AUTONOMY=advisory
   ```
2. Run `python -m agents.run` — it will now run through `AgenticAPIBrain`.
   If the endpoint is reachable, the agent loop executes tools and returns a
   summary; if not, `default_brain()` falls back to the rule planner and the
   demo keeps working.

Any OpenAI-compatible endpoint that supports **function calling** works:
OpenAI Chat Completions/Responses, Azure OpenAI, Groq, Mistral, OpenRouter,
together.ai, a local Ollama server, or a PSA-internal endpoint. If the API is
OpenAI-compatible but not identical, the only thing to adapt is the tiny
`AgenticAPIBrain._post()` + response-shape parsing in `agents/brain.py`.

### Step 1 — what is built today (the agent layer is live in the UI)

The agent layer is now wired into the dashboard — no headless-only gap:

- **`POST /api/agent/run`** (Phase 1) — send any goal through the runtime.
  The toolbar goal bar ("Send a goal to the agent…" + **▶ Run**) is the UI
  for it today: the question flows through the **same shared conversation
  thread as PSA Intelligence** (`POST /api/intel/ask`), so you can keep
  asking from any page and continue the chat on the PSA Intelligence page;
  the answer (and any **Approve / Reject** buttons) renders under the
  toolbar, and the run lands in the **Execution Trace**. Verified live with
  the LLM: "How many containers are at PSCH and how many MCC containers are
  bound for Antwerp?" → answered from `get_terminal_snapshot` with exact
  counts.
- **PSA Intelligence page** (Phase 0) — a chat-style prompting page (nav:
  **PSA Intelligence**). Ask anything; every answer is computed live through
  the same agent seam, and the conversation is logged so it survives reloads.
  With `AGENTIC_API_ENDPOINT` set, the footer shows `brain: llama` (the llama agent via Ollama);
  without it, `rule-based-intel-v1` answers instantly and deterministically.
- **Attention needed panel** (Phase 3) — on the Control Tower: the attention
  agent's live findings (receipt ETAs slipped by the road-delay layer,
  customs holds, outbound loading-window risk, vessel ETD slips), each with a
  recommendation. "Ask the agent" jumps to PSA Intelligence.
- **Approve / Reject** (Phase 4) — the agent (rule *or* LLM) can *propose*
  granular plan changes — `reassign_bin` ("move X to bin 5-08-1B"),
  `reschedule_receiving_area`, `release_lane` — but never executes them. The
  PSA Intelligence page shows **Approve / Reject** buttons; approval applies
  the change (traced), and every page reflects it on the next 8 s poll.
  Whole-batch plan writers are hidden from the LLM, so it can only make
  granular adjustments, never rewrite the plan set. When one answer returns
  **several proposals** (multi-action), each renders as its own labelled
  **proposal card** — `Proposal N · tool`, the concrete change (e.g.
  "Reassign MAEU4801288 → bin 5-08-1B"), and its own Approve/Reject pair — in
  both the chat thread and the toolbar box, so every pair clearly belongs to
  one specific change.

### Step 2 — the system of agents (what exists, what's next)

The brief wants *agents each serving a purpose*. Three are built, all on one
runtime (`AgentRuntime` + swappable `AgentBrain`), each one goal over the same
tool registry:

- **Planner agent** — `RuleBasedBrain` → `mcc_planner`: writes the plan
  proposals the whole hub displays.
- **Exception agent** — `ExceptionBrain` (`agents/exception.py`): watches the
  live state and proposes fixes (integration points D1–D3 of the map).
- **Q&A agent** — `IntelRuleBrain` / `AgenticAPIBrain`: the PSA Intelligence
  page.

Future agents (LCL delivery, transload, top-up) each need small new tables
(work orders) but follow the exact same pattern. The roadmap
(`AI_INTEGRATION_ROADMAP.md`) tracks what remains: **Phase 5** (live-data
polish + demo video) and wiring the exception agent's findings into
auto-re-proposals through `/api/agent/run`.

### Obstacles and gotchas to note

1. **Context budget.** Don't dump whole tables into the prompt — the system
   prompt caps the context snapshot at 4000 chars and the tool results at 3000
   chars each. Keep it that way; the LLM should *read through tools*, not
   receive everything upfront. (Cost + latency also stay sane.)
2. **Determinism vs. demos.** The rule brain is byte-for-byte reproducible;
   the LLM is not. For the recorded demo video, either (a) pre-record with the
   rule brain and *show* the LLM brain live afterwards, or (b) run the LLM
   brain with `SIM_SPEED=0` and fixed goals so replays are close. Never rely
   on the LLM for a scripted demo without a retry plan.
3. **Latency.** Function-calling loops take seconds per goal. The UI polls
   every 8 s; don't put a slow agent call in the poll path — make `/api/agent/run`
   an explicit user-triggered action.
4. **Tool-iteration runaway.** `AGENTIC_MAX_TOOL_ITERATIONS=20` bounds the
   loop; a model that keeps calling tools will hit it and return an error —
   that's a feature (it lands in the trace as `agent_run_end ok:false`).
5. **API keys.** Keep them in `.env` (git-ignored), never in the repo. The
   demo machine needs a reachable endpoint — for an offline venue, point
   `AGENTIC_API_ENDPOINT` at a local Ollama/LM Studio server so the demo works
   without internet.
6. **Approval flow.** In `advisory` mode, `approval`-level tools are returned
   as pending and never executed. If your demo needs to *show* an approval
   happening, call `AgentRuntime.approve()` from the UI (button) — don't flip
   autonomy to `autonomous` unless you want everything auto-applied.

### Deploying it

- **Hackathon demo (recommended):** run locally — `python server.py` on the
  demo machine. The app is a single stdlib HTTP server + SQLite file; there is
  nothing to configure beyond `.env`. Demo offline by pre-seeding a scenario
  (`SIM_SPEED=60` shows live movement; `SIM_SPEED=0` for a frozen, repeatable
  walkthrough).
- **Shared preview (judges on other machines):** run `server.py` bound to
  `0.0.0.0` (`PSA_HOST=0.0.0.0`) on a machine on the same network/LAN and open
  `http://<machine-ip>:8513`.
- **Public URL (optional):** any Python host works because the server is
  stdlib-only — Render, Railway, Fly.io, PythonAnywhere, or a VM. Run
  `python server.py` (it listens on `PSA_HOST:PSA_PORT`); optionally add a
  health check on `/api/health`. The Streamlit UI (`dashboard/app.py`) can be
  deployed to Streamlit Community Cloud with the same `requirements.txt`.
- **Data persistence:** `data/port.db` is created on first run and the world
  auto-regenerates, so there is no external database to provision. For a
  fresh demo, delete `data/port.db` and restart (or press **⟳ Regenerate**).
- **The video.** Judges see a ≤10 min video. Suggested arc: the terrarium
  running (SIM CLOCK ticking, stacker charges falling, journeys rolling) →
  select a container and walk the agent's reasoning → trigger `/api/agent/run`
  with a goal and show the trace → show an exception the agent flags → explain
  the seam (swap brains by config).

---

## 9. Configuration

Environment variables (see `config.py`; a `.env` file is read if present, see
`.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `SIM_SEED` | `42` | Random seed → same scenario every run |
| `N_CONTAINERS` | `400` | Total container population (≈85% PSCH-bound across the five flows) |
| `DRAYAGE_TOTAL` | `12` | Total trucks available for port↔PSCH moves |
| `SIM_NOW` | `2026-08-16T12:00Z` | Simulation clock anchor (in `config.py`) |
| `SIM_SPEED` | `60` | Sim-seconds per real second — how fast the world advances (`0` = frozen, deterministic) |
| `PSA_PORT` / `PSA_HOST` | `8513` / `0.0.0.0` | Classic UI listen address |
| `PSCH_RECEIVING_LANES` | `10` | Receiving areas/doors (numbered 1–10 in the UI) |
| `PSCH_AISLES_TOTAL` | `24` | Total rack aisles (ambient 1–21, cold room 22–24) |
| `PSCH_COLD_ROOM_AISLES` | `3` | Cold-room aisles at the end of the range |
| `PSCH_LEVELS_PER_AISLE` | `12` | Rack height in levels |
| `PSCH_BAYS_PER_LEVEL` | `3` | Bays per level (each with boxes A/B/C) |
| `PSCH_RELEASING_LANES` | `40` | Physical dispatch lanes (a cycling staging buffer, reused by non-overlapping groups) |
| `PSCH_PALLETS_PER_LANE` | `8` | Staged pallets one releasing lane holds |
| `MCC_GROUP_SIZE` | `6` | Max inbound MCC containers consolidated into one outbound box |
| `UNLOAD_MIN` / `YARD_TRANSFER_MIN` / `DEPOT_DWELL_MIN` / `ROAD_TRANSIT_MIN` | `180`/`45`/`120`/`45` | Port-side stage durations (minutes) |
| `STAGING_MIN` / `MOVE_TO_BIN_MIN` | `20`/`15` | PSCH receiving-side durations (minutes) |
| `AGENTIC_API_ENDPOINT` / `AGENTIC_API_KEY` / `AGENTIC_API_MODEL` | *(empty)* | Switch the agent brain to the external agentic AI API |
| `AGENTIC_AUTONOMY` | `advisory` | `advisory` / `semi_autonomous` / `autonomous` |
| `AGENTIC_MAX_TOOL_ITERATIONS` | `20` | Cap on agent tool-call loop iterations |

---

## 10. HTTP API (classic UI)

`server.py` serves the HTML page plus a small JSON API:

| Endpoint | Method | Purpose |
|---|---|---|
| `/` , `/index.html` | GET | The dashboard page |
| `/api/state` | GET | The full JSON view state (inbound/outbound lists, vessels, berths, facility, distribution, top-up, QC, KPIs, trace) |
| `/api/health` | GET | `{"ok": true}` liveness probe |
| `/map.png` | GET | The static berth-plan aerial graphic |
| `/static/*` | GET | Static assets |
| `/api/seed` | POST | Regenerate the scenario |
| `/api/agent/plan` | POST | Re-run the planner agent |
| `/api/trace/clear` | POST | Clear the execution trace |
| `/api/berth/inspect` | POST | Log a berth inspection (trace) |
| `/api/psch/inspect` | POST | Log a rack inspection (trace) |

---

## 11. Headless / CLI usage

```bash
python -m data.seed --seed 42 --containers 400   # generate + load a scenario
python -m agents.run                            # run the planner, print + persist
```

---

## 12. Project layout

```
config.py                      simulation clock, scenario size, planning
                               constants, agentic AI API settings
models/schemas.py              pydantic models: vessels, containers (with stow
                               cells), bookings, shipments, MccPlan,
                               OutboundContainer, ...
data/simulator.py              seeded synthetic world (fleet, bay plans, cargo,
                               pallets, five hub flows)
data/store.py                  SQLite data layer shared by planner + both UIs
data/seed.py                   generate + load a scenario (CLI)
data/facility.py               PSCH facility model (rooms, aisles, bins,
                               staging lanes, the space-utilisation view)
agents/tools.py                declarative tool registry (the agentic AI seam)
agents/brain.py                AgentBrain protocol: rule-based + agentic-API brains
agents/runtime.py              AgentRuntime: run loop, permission gates, trace
agents/mcc_planner.py          the deterministic planning agent (journey,
                               receiving, putaway/slotting, consolidation) +
                               the road-delay layer (delay_hours)
agents/intel.py                PSA Intelligence rule brain (Q&A + plan proposals)
agents/exception.py            exception agent (scan + ExceptionBrain)
agents/run.py                  headless planner runner
analysis/kpis.py               control-tower KPIs
analysis/bayplan.py            vessel bay-plan visualisation (Bay-Row-Tier)
analysis/psch_view.py          PSCH facility view CSS/HTML helpers
server.py                      classic early-2000s UI + stdlib JSON API
dashboard/app.py               alternative Streamlit UI
Map.png                        static aerial berth-plan graphic
PORT_PROCESS_FLOW.md           domain reference: how a container port works
PROJECT_SPEC.md                the full project design specification
SOLUTION.md                    solution write-up (architecture, decisions)
HACKATHON_BRIEF.md             the competition brief this project answers
AGENTIC_AI_ARCHITECTURE.md     how the agentic AI API plugs in
AGENTIC_AI_INTEGRATION_MAP.md  living checklist of AI integration points
AI_INTEGRATION_ROADMAP.md      phased build plan (Phases 0–4 done, 5 remaining)
AGENT_ONBOARDING.md            implementation map for new agents/developers
```

---

## 13. Tests

```bash
python -m pytest        # 92 tests: simulator, facility, planner, frontend, trace,
                        # agent runtime (registry, gates, granular tools, LLM gating),
                        # intel (Q&A + plan proposals + memory), exception agent
```

---

## 14. Design notes and limitations

- **Synthetic only.** All data is modelled on realistic port/CFS structures —
  there is no real CITOS/PORTNET access, no live GPS, no real vessel tracking.
  The map is a static graphic with simulated occupancy.
- **The agent plans; it does not execute.** Plans are written as proposals
  through the tool layer; nothing in the physical world moves because of a
  button press.
- **Deterministic by default.** The rule-based brain means any demo can be
  replayed exactly; switching to the agentic API is a configuration change, not
  a code change.
- **The clock is live but simulated.** The world advances at `SIM_SPEED`× real
  time and auto-regenerates a new wave when a lifecycle completes, so left
  running it behaves like a living terrarium. For a fully deterministic replay
  (same timestamps every run), set `SIM_SPEED=0`.
- **Two UIs, one world.** The Streamlit dashboard shares the store and agent but
  has not been restructured to the hub's five-page layout; the classic UI is
  the full hub experience.

## 15. Glossary (quick reference)

| Term | Meaning |
|---|---|
| Container | Standardised steel box for moving cargo (20ft / 40ft / 40HC) |
| TEU | Twenty-foot equivalent unit — the base unit of container capacity |
| FCL / LCL | Full Container Load / Less than Container Load |
| MCC | Multi-country consolidation — cargo grouped across shippers/countries |
| Transload | Moving cargo from one container to another |
| Top up | Adding more cargo into a partially filled container |
| Deconsolidation / consolidation | Unpacking a container / packing cargo together |
| Berth | A ship's parking spot alongside the quay |
| Discharge | Lifting containers off a ship |
| Depot yard | The port's open storage area for containers |
| Drayage / prime mover | Container trucking / the truck that pulls a container |
| AS/RS / stacker | Automated Storage & Retrieval System / the robot that moves pallets into and out of the racks |
| ETA / ETD | Estimated time of arrival / departure |
| Stowage, Bay-Row-Tier | How a ship's containers are arranged, and how one cell is addressed |
| Rack / aisle / bin | Warehouse shelving / row of racks / one pallet location |
| Receiving lane / releasing lane | Door where containers unload / dock position where outgoing cargo stages |
| Reefer | Refrigerated container or cargo |
| DG / Hazmat | Dangerous goods |
| OOG | Out of gauge — oversized cargo |
| CFS / DC | Container freight station / distribution centre |
| Customs status | Cleared / pending / held by customs |
