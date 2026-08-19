# Agentic AI Integration Map — every integration point, with feasibility

> **Standing rule for all feature work:** when implementing any feature, update
> this map with the areas where agentic AI can be integrated. Agentic AI is
> intended to be used **across the whole software in more than one area**, so
> every integration point below lists where it hooks into the code, what data
> it needs, its autonomy/risk profile, and a **feasibility rating** (High /
> Medium / Low) with the reasoning.
>
> The enabling foundation already exists: `agents/tools.py` (tool registry),
> `agents/brain.py` (swappable brain incl. the agentic AI API seam), and
> `agents/runtime.py` (run loop + permission gates + trace). Every area below
> is reachable through that seam — no area requires a separate AI system.

## Feasibility rubric

| Factor | High | Medium | Low |
|---|---|---|---|
| Data available today | All needed state in `store`/simulator | Some fields missing, easy to add | Needs real external feed or new capture |
| Risk of wrong action | Advisory / reversible | Mutates a plan (reviewable) | Executes real-world action |
| Fit for agentic reasoning | Judgment-heavy, messy, multi-feed | Structured, mostly algorithmic | Purely deterministic math |
| Effort | Reuses registry + one brain/goal | New tools + new schema | New subsystem |

---

## A. Orchestration & planning layer (`agents/`)

| # | Integration area | Hooks into | Data needed | Autonomy / risk | Feasibility |
|---|---|---|---|---|---|
| A1 | **MCC planning** (journey timeline, receiving, slotting, consolidation) | `mcc_planner.py` (exists, rule-based) | vessels, containers, stowage, shipments, bins, lanes | advisory / low | **High — already built**; the LLM brain can take over via the same tools |
| A2 | **LCL deconsolidation** (unstuffing plan, dock door, staging, route to delivery vs. re-consolidation) | new `agents/lcl.py` on `AgentRuntime` | containers (import/LCL), bookings, shipments, dock-door state | advisory / low | **High** — same pattern as A1; needs an `unstuff_work_orders` table |
| A3 | **Local LCL delivery dispatch** (vehicle allocation, route sequence, ETA, POD) | new `agents/delivery.py` | shipments ready, drayage fleet, destinations, POD records | semi-autonomous / low-med (reversible) | **High** — drayage table already exists; add delivery orders + POD |
| A4 | **Transloading** (work orders, reason codes, rework tracking, cost recovery) | new `agents/transloading.py` | containers, work orders, damage/inspection records, costs | advisory / med (releases a container) | **High** — pure judgment fit; `approval` gate on replacement-container release |
| A5 | **FCL visibility & handoff** (track a shipper-exclusive container; cut-off/customs alerts) | `mcc_planner` journey logic reused for `import`/`export` flags | containers, vessels, customs status | advisory / low | **High** — schema already carries FCL containers; mostly read + alert tools |
| A6 | **Container topping-up** (suggest cargo to fill under-filled outbounds) | new goal over `outbound_containers` + `shipments` | outbound fill %, available cargo, vessel cutoff | advisory / low | **High** — derivable today; needs a fill-% tool |
| A7 | **Cross-function orchestrator** (a goal that chains A1–A6 tools, e.g. "deconsolidate, dispatch local, top up the Antwerp FCL") | `AgentRuntime` with a multi-step goal | all of the above | advisory (escalates) | **Medium-High** — the LLM brain is the natural orchestrator; needs prompt + tool discipline |
| A8 | **Dynamic re-planning on exceptions** (ETA slip, missed cutoff, customs hold → re-plan affected containers) | re-invoke planner on event; diff old vs new plan | event stream (trace), plans | advisory / med | **High** — the highest-value agentic behaviour; needs the event stream (D1) |

## B. Data ingestion & integration adapters (`integrations/`)

| # | Integration area | Hooks into | Data needed | Autonomy / risk | Feasibility |
|---|---|---|---|---|---|
| B1 | **Feed normalization & mapping** (CITOS/PORTNET/TradeNet/AIS → internal model) | adapter layer → `store` | real feeds (simulated today) | advisory / low | **High** — LLMs excel at messy schema mapping; adapters are drop-in |
| B2 | **ETA / delay anomaly detection & reconciliation** (flag implausible ETA jumps, reconcile vessel vs. plan) | adapter layer, `vessels` | live ETA feeds, history | advisory / low | **High** — judgment-heavy, low risk |
| B3 | **Data-quality handling** (missing fields, partial feeds → graceful degrade) | adapter layer, `store` | feed completeness signals | advisory / low | **High** — matches the brief's "uncertainty" requirement |

## C. Decision support / optimization

| # | Integration area | Hooks into | Data needed | Autonomy / risk | Feasibility |
|---|---|---|---|---|---|
| C1 | **Receiving-area & dock-door allocation** (exists as a rule) | `mcc_planner.py` | arrival rate, dock state | advisory / low | **High** — rules are fine; agent handles edge cases (peaks, special cargo) |
| C2 | **Bin slotting** (exists as a dwell-height rule) | `mcc_planner.py` | dwell prediction, cargo type | advisory / low | **High** |
| C3 | **Consolidation grouping** (exists as a destination+cutoff rule) | `mcc_planner.py` | destinations, cutoffs, fill | advisory / low | **High** — agent can beat the greedy rule on fill % |
| C4 | **Route / vehicle optimization for local delivery** | `agents/delivery.py` | stops, fleet, time windows | semi-autonomous / low | **Medium** — pure VRP is better as a deterministic solver; agentic for exceptions and last-mile judgment |
| C5 | **Resource/equipment scheduling** (robots, AGVs, labour) | `data/facility.py` | equipment state | advisory / med | **Medium** — needs equipment-state data; agentic only for the messy bits |

## D. Exception handling & monitoring ⭐ (best agentic fit)

| # | Integration area | Hooks into | Data needed | Autonomy / risk | Feasibility |
|---|---|---|---|---|---|
| D1 | **Event stream + exception detection** (cargo arrived, cutoff approaching, damage, dwell overrun, customs hold) | `trace` / new `events` table | typed events, plans, statuses | advisory / low | **High — built** — `agents/exception.py` (`scan_exceptions` for the 8s poll, `find_exceptions` for traced runs) + a deterministic road-delay layer in `mcc_planner` so the synthetic world genuinely produces exceptions |
| D2 | **Triage & mitigation proposal** (rank exceptions, propose fix, escalate) | `AgentRuntime` on event | D1 events + plans | advisory / med | **High — built** — `ExceptionBrain` ranks by severity and recommends (receipt slips → re-sequence receiving; customs holds → hold; loading windows → expedite; ETD slips → expedite) |
| D3 | **Proactive alerts with reasoning** ("why is this at risk, what I recommend") | dashboard + notifications | D1/D2 output | advisory / low | **High — built** — Exception Watch panel on the Control Tower + `/api/agent/exceptions` + "Ask the exception agent" shortcut |

## E. Customer-facing

| # | Integration area | Hooks into | Data needed | Autonomy / risk | Feasibility |
|---|---|---|---|---|---|
| E1 | **Track-and-trace natural-language Q&A** ("where is my cargo / when does it reach PSCH?") | new `agents/assistant.py` over the registry | containers, plans, outbounds | read-only / low | **High** — the tool registry is already the query surface |
| E2 | **Booking / quote assistant** | `bookings`, pricing | bookings, rates | advisory / low | **High** |
| E3 | **Proactive notifications** (cut-off, delay, POD) | D3 output | D1/D2 | advisory / low | **High** |

## F. Dashboard / UX (`server.py`, `dashboard/app.py`)

| # | Integration area | Hooks into | Data needed | Autonomy / risk | Feasibility |
|---|---|---|---|---|---|
| F1 | **Natural-language query over the dashboard** ("show containers delayed >6h") | new `/api/agent/query` → registry tools | all store tables | read-only / low | **Medium** — needs NL→tool mapping; registry makes it tractable |
| F2 | **Auto narrative summaries** ("what changed in the last 24h") | `/api/state`, KPIs, trace | plans, trace, KPIs | advisory / low | **High** |
| F3 | **Plan explanation** ("why this bin / lane / vessel") | existing `reasoning` strings | plans | read-only / low | **High** — already half-built (reasoning fields) |
| F4 | **PSA Intelligence prompting page** ("ask anything about the terminal") | new `agents/intel.py` (`IntelRuleBrain`) + `/api/intel/ask` → `AgentRuntime` | all store tables via the tool registry | advisory / low | **High — built**; rule brain answers live today, `default_intel_brain()` swaps to the LLM via config (Phase 2 of `AI_INTEGRATION_ROADMAP.md`) |

## G. Reporting & analytics (`analysis/`)

| # | Integration area | Hooks into | Data needed | Autonomy / risk | Feasibility |
|---|---|---|---|---|---|
| G1 | **KPI anomaly explanation** (why did bin util jump?) | `kpis.py` | KPI history, plans | advisory / low | **High** |
| G2 | **Auto exception reports with recommendations** | `kpis.py` + D2 | KPIs, exceptions | advisory / low | **High** |

## H. Simulation / what-if / scenario

| # | Integration area | Hooks into | Data needed | Autonomy / risk | Feasibility |
|---|---|---|---|---|---|
| H1 | **Scenario generation** for demos/training | `data/simulator.py` | scenario params | advisory / low | **Medium** — nice-to-have; deterministic generator already works |
| H2 | **What-if analysis** ("if the vessel slips 6h, what re-plans?") | `AgentRuntime` on a cloned store | plans, vessels | advisory / low | **Medium-High** — needs a clone-store tool |

## I. Governance & audit

| # | Integration area | Hooks into | Data needed | Autonomy / risk | Feasibility |
|---|---|---|---|---|---|
| I1 | **Trace summarisation & compliance check** (did the agent follow policy?) | `trace` | trace | read-only / low | **High** |
| I2 | **Plan quality self-evaluation** (score plans, flag weak spots) | `BrainResult` | plans, outcomes | advisory / low | **Medium** |

---

## Feasibility verdict

* **12 of 22 areas are High**, and the highest-value ones (A8 dynamic re-planning,
  D1–D3 exception handling, A2–A4 the other CFS functions, E1 customer Q&A) are
  all High. This confirms agentic AI can be used across the whole software in
  many places, not one.
* **Every High area reuses the existing seam** (registry + brain + runtime) —
  the marginal cost of each new area is one brain/goal plus (for some) a new
  table, not a new system.
* **The two real dependencies are not code but data**: a typed event stream
  (D1, unlocks A8/D2/D3) and, for production, the real CITOS/PORTNET/TradeNet/
  AIS feeds (B1–B3). Both are additive — the simulated store is the contract.
* **Medium areas** (C4 route optimization, F1 NL-query, H1 scenario gen) are
  better as deterministic algorithms with agentic *oversight*, or need extra
  capture — do them after the High set.

## Recommended phasing

1. **Now (foundation, done)** — tool registry, swappable brain, runtime gates, trace.
2. **Next (High, no new data)** — A2 LCL deconsolidation, A4 Transloading,
   A5 FCL visibility, A6 topping-up, F3/E1 explanations & Q&A (F4 built; A8 dynamic
   re-planning built as Phase 4 granular change tools).
3. **Then (High, unlocks the flagship)** — D1–D3 exception agent ✅ built
   (`agents/exception.py` + Exception Watch panel); next: wire exception-driven
   auto re-proposals into `/api/agent/run` so the agent acts on its own findings.
4. **Later (needs real feeds)** — B1–B3 adapters against live CITOS/PORTNET/
   TradeNet/AIS; then C4/F1/H1–H2 as agentic-oversight features.

## Cross-cutting concerns (apply to every area)

* **Permission model**: `read` always; `mutate` = plan proposal (reviewable);
  `approval` = real-world execution (human-gated). Default is **advisory**.
* **Auditability**: every agent tool call, decision and approval lands in the
  `trace` — the same audit trail covers all areas, satisfying the brief's
  trace requirement facility-wide.
* **Cost/latency**: the rule brain stays as the zero-cost fallback; the LLM
  brain only runs where judgment adds value. Keep context bounded by reading
  through tools, not dumping whole tables into the prompt.
* **Determinism**: keep the rule brain for reproducible demos; the LLM brain is
  the production path. Both share one trace shape.
