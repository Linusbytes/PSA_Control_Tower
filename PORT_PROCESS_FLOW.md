# Container Port Process Flow — Domain Reference

> Reference document for building this project. Captures how a container port
> operation actually works, stage by stage, plus the terminology and metrics a
> real terminal dashboard would track. All data in this project remains
> synthetic; this file just grounds the model and UI in realistic domain
> knowledge. Sources are noted inline where relevant.

## 1. The flow, stage by stage

The import flow runs top-to-bottom; export runs the same sequence in reverse
(truck gate-in → yard stacking → customs declaration → quay crane load), so the
same terminology applies both ways.

### 1.1 Approach and pre-arrival

- Days before arrival, the vessel's voyage is already being tracked via **AIS**
  (Automatic Identification System) feeding into the port's **vessel traffic
  system**.
- **PORTNET** integrates with MPA's **Next Generation Vessel Traffic Management
  System (VTMS)** for real-time situational awareness of shipping traffic — the
  port's vessel traffic system provides accurate, real-time situational
  awareness by integrating with PORTNET.
- Key terms:
  - **ETA** — estimated time of arrival
  - **ETB / ATB** — estimated / actual time of berth
  - **Berth window** — the slot the terminal has planned for that vessel based
    on its own scheduling engine

### 1.2 Pilotage and berthing

- A pilot boards, tugs assist, and the vessel moors.
- Dashboards track:
  - **Vessel waiting time** — time from arrival in port to mooring at berth.
    Long waiting times are the first sign of a berth-allocation mismatch.
  - **Berth occupancy** — how much of the available berth time is actually
    being used.

### 1.3 Discharge

- **CITOS** (Computer Integrated Terminal Operations System) — PSA's core
  planning engine, the "brain of the terminal": using expert systems, it plans
  the use of berth, yard, equipment and manpower, transmitting work
  instructions to every machine operator in real time over a wireless data
  network.
- Quay cranes work against a bay/stowage plan.
- Dashboard metrics — the most-watched in the whole operation:
  - **GMPH / NMPH** — gross/net moves per hour (net excludes recorded delays)
  - **Berth productivity** — moves per berth hour; factors in how many cranes
    are working the vessel simultaneously
  - **Crane intensity** — cranes assigned vs. promised
- Controllers watch per-crane rate and overall moves-per-vessel-hour together,
  not just one: a terminal can show a high per-crane rate but low overall
  vessel throughput if crane assignment, scheduling, or yard coordination is
  off.
- At Tuas specifically, quay-side automation is furthest along on cargo
  handling, but PSA has flagged that lashing/unlashing still needs a person —
  even at the world's largest automated container terminal, vessel-side
  automation runs into lashing and unlashing requirements that necessitate
  human-in-the-loop systems.

### 1.4 Yard transfer and stacking

- Containers move from quay to yard by **AGV** (automated guided vehicle) or
  prime mover, then get placed by a yard crane:
  - **RTG** — rubber-tyred gantry
  - **RMG** — rail-mounted gantry
  - **ASC** — automated stacking crane
- Dashboard terms:
  - **Yard utilization / density** — % of slots occupied
  - **Re-handles / reshuffles per move** — the number of non-productive moves
    needed to dig out a target container; a direct signal of how well the yard
    was slotted in the first place

### 1.5 Customs and dwell

- **Dwell time** is the clock that matters most to cargo owners:
  - **Import dwell** — measured from the discharge timestamp to the gate-out
    timestamp
  - **Export dwell** — measured from gate-in to load
- **TradeNet** (Singapore's customs declaration system) and PORTNET's
  documentation flows do the paperwork that must clear before a box is
  released — PORTNET has integrated with CITOS and TradeNet to provide a single
  window for cargo and vessel information.
- Dwell time quietly drives cost:
  - Every day past the **shipping line's free time** → **demurrage**
  - Every day past the **terminal's free time** → **storage/detention charges**

### 1.6 Gate-out and pickup

- The haulier books a slot (often through an appointment system tied to
  PORTNET), truck gate-in/gate-out is logged, and the container is released.
- Dashboard terms:
  - **Truck/gate turnaround time** — time a truck spends in the terminal;
    leading terminals target under 45 minutes
  - **Gate throughput** — trucks or moves processed per hour
- This closes the loop — the container is now with inland transport.

## 2. Key metrics at a glance

| Metric | What it measures | Why controllers watch it |
|---|---|---|
| Vessel turnaround time | Port arrival to port departure | Overall schedule reliability |
| Vessel waiting time | Arrival to mooring at berth | Flags berth-allocation congestion |
| Berth occupancy | % of berth-time used vs. available | Capacity planning |
| GMPH / NMPH | Crane moves per hour, gross vs. net of delays | Crane and labor productivity |
| Berth productivity | Moves per berth hour (accounts for crane intensity) | True vessel service speed |
| Yard utilization / density | Occupied slots ÷ total slots | Space pressure, congestion risk |
| Container dwell time | Discharge→gate-out (import) or gate-in→load (export) | Customs/trucking bottlenecks, demurrage exposure |
| Re-handles per move | Non-productive crane moves to reach a box | Yard slotting quality |
| Truck/gate turnaround time | Time a truck spends in the terminal | Landside congestion, haulier experience |
| TEU throughput | Total containers processed (20-ft equivalent) | Overall volume against capacity |

## 3. The systems behind the terminology

- **CITOS** — the planning and execution engine: berth, yard, crane, and
  manpower allocation. Essentially the operational "brain." (Terminal
  operations system.)
- **PORTNET®** — now extended as **PORTNET+/CALISTA** (Cargo Logistics,
  Inventory Streamlining & Trade aggregation). The community/EDI layer
  connecting shipping lines, hauliers, freight forwarders and government
  agencies for bookings, documentation, and track-and-trace.
- **CIMOS** (Computer Integrated Marine Operations System) — the parallel
  legacy system on the marine side (vessel movements, tugs, pilotage).
- **TradeNet** — Singapore's customs declaration system.
- **AIS / VTMS** — Automatic Identification System feeding the port's vessel
  traffic system; MPA's Next Generation Vessel Traffic Management System.

One older paper put it well: **CITOS is the "brain" of terminal operations,
while PORTNET is the data and information "circulatory system."** PSA's current
public framing wraps both into what they now call **Control Tower**
architecture — a system that integrates data from diverse sources to enable
real-time visibility and operational decision-making across the port.

## 4. Notes for building

- Where this project touches port-side concepts (discharge events, yard
  status, vessel cutoffs, dwell), use this file's terminology so the dashboard
  and agent speak the domain language: e.g. `dwell_time` computed as
  `gate_out − discharge` for import, `cargo_ready − gate_in` style framing for
  export.
- The handoff to PSCH (deconsolidation) happens after customs clearance and
  release — i.e. between §1.5 and §1.6 of the flow above. Dwell time is the
  natural KPI for justifying that handoff.
- Vessel waiting time and berth productivity are the classic "port-side
  health" metrics; GMPH vs. NMPH separation (gross vs. net of recorded delays)
  is a useful modeling pattern for the synthetic data generator.
