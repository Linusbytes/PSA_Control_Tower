# PSA Control Tower — Software Guide & Change Log

A self-contained guide to the whole software: what it does, everything that was
built or changed up to this point, a dedicated chapter on **how the AI works**,
and an honest list of **limitations and workarounds**.

> Companion docs: `README.md` (beginner tour), `AGENT_ONBOARDING.md` (agent
> handoff / implementation map), `AGENTIC_AI_ARCHITECTURE.md` (AI design
> contract), `AI_AGENT_QA_PROMPTS.md` (317-question QA suite), `PORT_PROCESS_FLOW.md`
> (domain model), `HACKATHON_BRIEF.md`.

---

## Chapter 1 — The full functionality suite

### 1.1 What the software is

The **PSA Control Tower** is a live synthetic simulation ("terrarium") of
**MCC (Multi-Country Consolidation) cargo** moving between **Tuas Port** and the
**PSCH container freight station**. A deterministic planner agent derives every
container's journey (vessel ETA → unload → depot → road → PSCH receipt →
receiving → putaway → storage → picking → releasing → outbound consolidation),
and an AI agent layer answers questions and proposes plan changes on top of the
same data. Everything runs at **60× sim speed** (`SIM_SPEED`, default 60), so
the world moves by itself; no button is needed to "make it live".

Runs as a **Python stdlib HTTP server** (`server.py`, no framework) serving a
Windows-95-themed single-page UI on **port 8513**, backed by one **SQLite file**
(`data/port.db`) that auto-seeds on first run. A Streamlit dashboard
(`dashboard/app.py`) shares the same data layer and planner.

### 1.2 The pages

| Page | What it does |
|---|---|
| **PSCH Inbound** | Master list of every container bound for PSCH, searchable and filterable by flow (All / MCC / Distribution / Top Up / Transload). Live KPI strip, berth map ("Viewer — click on a berth to inspect it"), and a Vessel & Container Details inspector with the on-demand **stowage plan** (Bay-Row-Tier). |
| **PSCH Storage** | The receiving/putaway playground: **Inbound Cargo by Container ID** (search + description "Includes Bin Location and Stacker responsible"), numbered receiving lanes (1–10) and releasing lanes (1–40), the AS/RS rack view with aisles/levels/bins, stacker states (working/charging), occupancy stats (AMBIENT / COLD ROOM, bin utilisation, pallets planned vs in storage). |
| **Outbound ▸ MCC** | Back-to-port consolidation containers: destination grouping, bound vessel, loading cell, stowage plan with the loading target. |
| **Outbound ▸ Distribution** | LCL/FCL land releases with search, release lanes, destinations. |
| **Outbound ▸ Top Up** | Container re-consolidation jobs with search, transfer windows. |
| **Quality Control** | QC task board (survey / sampling / repack / rework) with windows and stations. |
| **Control Tower** | KPI strip for the MCC cargo pipeline + the live **Attention needed** panel (agent findings grouped by category, with one-click AI-suggested actions). |
| **Execution Trace** | The audit trail, grouped by actor, filterable, with an **AI Changes** view and a live indicator. |
| **PSA Intelligence** | The chat page: ask anything about the data; the agent answers live (rule brain or LLM), proposes plan changes with **Approve / Reject**, and remembers the conversation. |

### 1.3 Global features

- **SIM CLOCK** in the top bar — shows sim date/time **with seconds** (`2026-08-16 13:09:42Z`).
- **Toolbar (left, 4 buttons):** `↻ Regenerate` (re-seed the world), `▶ Run Hub
  Planner` (re-run the planning agent), `⛶ Full Screen`, `⚙ Display` (flash
  display settings).
- **Toolbar goal bar (right):** "Send a goal to the agent…" + `▶ Run` — multi-turn,
  shares the same conversation thread as PSA Intelligence.
- **Navigation:** PSCH menu (Inbound / Storage / Outbound ▸ MCC · Distribution ·
  Top Up / Quality Control), Control Tower, Execution Trace, PSA Intelligence.
- **Sidebar (PSA Intelligence):** `Ask` (chat), `Log` (trace), `Ctrl` (control
  tower), and a profile-avatar placeholder at the bottom.
- **Bloomberg-style value flashes:** cells briefly flash green/red when a live
  value rises/falls; speed, brightness and palette are adjustable under Display.
  Receiving-lane blocks flash when containers arrive or leave a lane.
- **Search bars** on every relevant sub-page (Inbound, Storage, MCC, Distribution,
  Top Up), with live dropdowns and flow filters.
- **Ticker strip** ("LIVE · 24/7 · Port ↔ PSCH flow") and a status bar.

### 1.4 The synthetic ecosystem

- **400 containers** (`N_CONTAINERS`, default) across five flows:
  MCC ~36%, LCL ~26%, FCL ~16%, Top Up ~14%, Transload ~8%.
- **Vessels, bookings, shipments, yard blocks, drayage trucks, SLA profiles** —
  one coherent cargo story, seeded deterministically (`SIM_SEED=42`).
- **Waves:** when the last outbound container of a wave loads, the world
  auto-regenerates the next wave relative to the live clock (full lifecycle loop).
- **Warehouse:** 24 aisles (ambient 1–21, cold room 22–24, aisle 21 hazmat),
  12 levels × 3 bays × A/B/C = **2,592 bins**, dwell stock from "previous waves"
  so aisles sit in a realistic 30–70% utilisation band.
- **Receiving:** 10 numbered lanes/doors; **releasing:** 40 lanes as a cycling
  staging buffer; **8 AS/RS stackers** with a staggered 24/7 charge rotation
  (2 charging bays).
- **A deterministic road-delay layer:** ~12% of plans slip 1–8h past their
  promised receipt ETA (so the exception agent has real events to find).

---

## Chapter 2 — Change log (everything built or updated up to this point)

Grouped by area. Each item is one shipped change; "why" is noted where useful.

### 2.1 Simulation & data

- **Denser synthetic population** — 400 containers across all five flows so
  every aisle reaches a realistic 30–70% utilisation (some ~10%), with 24/7
  arrivals spread across the day rather than shift-banded; a dwell-stock floor
  keeps occupancy realistic from wave one.
- **Live-clock backend** — all statuses derive from `sim_now()` (process-anchored,
  60×); the world advances on its own.
- **Auto wave regeneration** — when a wave completes, the next wave is seeded
  relative to the live clock and re-planned (the terrarium loop).
- **Road-delay layer** (`delay_hours`, ~12% of plans, 1–8h) with `journey_status`
  using the delayed arrival so every page stays consistent — added so the
  exception agent finds *genuine* exceptions instead of a perfect clockwork world.

### 2.2 PSCH Storage page

- **Process-flow colours unified** across inbound → receiving → storage → outbound
  for easier viewing.
- **Renames:** "unloading and putaway - inbound cargoes" → **"Inbound Cargo by
  Container ID"** (description below: *"Includes Bin Location and Stacker
  responsible"*); the "Select a container to open its receiving & putaway plan"
  description moved directly under the header.
- **Receiving lanes numbered** (1–10, no more "Receiving RA-1" labels).
- **Lane rectangles widened** so container numbers fit on one line without
  wrapping; horizontal scroll when needed; lanes narrowed back to match the
  font (numbers centre-aligned).
- **Receiving-lane blocks flash** when containers arrive or leave a lane (same
  pattern as releasing lanes). Container details for receiving lanes are
  **disabled by design** — lanes are unclickable but fully viewable.
- **Storage rack search/selection** reflects approved plan changes live.

### 2.3 UI & theme

- **Toolbar rework:** 4 buttons grouped left (Regenerate · Run Hub Planner ·
  Full Screen · Display); the goal bar moved to the far right at 11px theme font
  with a `▶ Run` button; the "PLANNER: rule-based agent… seed 42" text removed.
- **Sidebar rework:** Ask / Log / Ctrl only; "PSA AI" brand box and settings gear
  removed; **profile-avatar placeholder** in the You circle.
- **Search bars applied** to all relevant sub-pages (Inbound pattern replicated
  on MCC, Distribution, Top Up).
- **Full Screen** keeps its two symbols (enter/exit); buttons packed together.
- **Stowage-plan flicker fixed:** the on-demand `/api/bayplan` HTML is cached per
  container and now **re-applied on every 8s poll**, so plans no longer vanish
  back to "loading stowage plan…".
- **MCC definition grounded:** the agent now defines **MCC = Multi-Country
  Consolidation** (never "merged/mixed") in both the LLM system prompt and the
  rule brain's help/flow answers.
- **Agent reasoning panels restructured:** the plan `reasoning` strings are now
  generated as labelled lines (Journey / Timeline / Receiving / Flow / Plan /
  Staging / agent-change notes / road-delay warns) and rendered as tidy
  two-column rows in every Agent reasoning panel (Storage, Inbound, Outbound,
  Top Up) instead of one jumbled paragraph.
- **Multi-action answers render as proposal cards:** when the agent returns
  several plan changes in one reply, each pending proposal is its own bordered
  block (`Proposal N · tool` header + change label + its own **Approve /
  Reject** pair) in both the PSA Intelligence thread and the toolbar goal box,
  and the model's numbered recap lines are stripped from the answer prose (the
  cards carry that information).

### 2.4 Execution Trace

- **Timestamps follow the simulation clock**, not the wall clock — the trace
  lives in the same time as the world it records.
- **Sim clock and trace rows show seconds** (HH:MM:SS).
- **Categorised by actor** — each actor (AGENT / RUNTIME / OPERATOR / SYSTEM) is
  its own grouped section with coloured badges.
- **Filter chips with live counts:** All · AI Changes · Agent · Runtime ·
  Operator · System.
- **AI Changes view** — the full lifecycle of every plan change:
  `approval_required` (proposal) → `approved`/`rejected` (your decision) →
  `tool_call` (execution), with **PENDING / APPROVED / REJECTED** badges and
  friendly summaries ("MAEU4801288 → bin 5-08-1B") instead of raw JSON.
- **Backend completeness:** every pending proposal is now traced as
  `approval_required` from the runtime (one recording site for rule-based and
  LLM brains) — previously rule-brain proposals left no audit trail.
- **Live indicator:** `● LIVE` in the header, gently blinking, flashing brighter
  the instant new events arrive.

### 2.5 AI integration (the agentic layer)

- **Phase 0 — PSA Intelligence page** (chat) with a rule-based intel brain.
- **Phase 2 — LLM brain activated** via Ollama (`qwen2.5:7b`), OpenAI-compatible
  endpoint; footer shows `llama` (the llama agent).
- **Anti-hallucination hardening:** a `get_terminal_snapshot` tool returns the
  *authoritative* KPIs (the same numbers the dashboard renders) and the system
  prompt forbids inventing counts; verified to stop the model fabricating
  container totals from utilisation percentages.
- **Phase 1 — goal endpoint** `POST /api/agent/run` (the toolbar `Run` button
  now routes through the shared chat thread instead).
- **Phase 3 — attention agent** (`agents/exception.py`): `scan_exceptions` feeds
  the 8s poll without flooding the trace; `find_exceptions`/`ExceptionBrain`
  power agent runs and the **Attention needed** panel with recommendations.
- **Phase 4 — the agent takes action, granularly:** `reassign_bin`,
  `reschedule_receiving_area`, `release_lane` (one plan field each, whitelisted
  in-place UPDATEs), gated behind **Approve / Reject**; the whole-batch writers
  (`save_mcc_plans`, `save_outbound_containers`) are hidden from the LLM
  (`expose_to_llm=False`), and the LLM brain returns every mutate/approval tool
  as `pending_approval` — it cannot rewrite the plan set.
- **Conversational memory:** the last ~10–12 messages of the thread travel with
  every question (rule brain resolves follow-ups like "and what about its
  vessel?" against the last-mentioned container; the LLM receives them as real
  chat messages). The toolbar bar and PSA Intelligence share one thread.
- **QA suite:** `AI_AGENT_QA_PROMPTS.md` — 317 prompts across facts, boundaries,
  context/logic, security/tone, plan-change capability, and a new **multi-action
  family (F)** that tests several plan changes in one run and their consequences.
  Multi-action answers render as one labelled **proposal card per change**, so
  the Approve/Reject pair for each container is unambiguous even with 4+ pending
  proposals in one reply.

---

## Chapter 3 — How the AI works (dedicated chapter)

### 3.1 The one pipeline

Every AI behaviour in the software runs through the same pipeline:

```
goal / question / operational event
        │
        ▼
AgentRuntime.run(goal, context, history)
        │  records agent_run_start
        ▼
Brain.run(goal, context, store_path, history)
        │  (one of the four brains below)
        ▼
call_tool(name, args, path)      ← the single funnel
        │  every call → "tool_call" trace event (actor: agent)
        ▼
permission gate (autonomy)       ← READ allowed; MUTATE/APPROVAL gated
        │
        ▼
result → agent_run_end trace event → response (answer / proposal / summary)
```

**Key idea:** the brain is swappable. The runtime, tools, trace and approval
gates are identical whether the decision engine is a deterministic planner, a
rule-based chat brain, or an external LLM.

### 3.2 The four brains

| Brain | File | When it runs | Notes |
|---|---|---|---|
| `RuleBasedBrain` (MCC planner) | `agents/mcc_planner.py` | Run Hub Planner / auto on fresh world | Deterministic; writes the plan batch through the tools. |
| `IntelRuleBrain` | `agents/intel.py` | PSA Intelligence when no LLM configured | Keyword-intent Q&A over live data; proposes changes; resolves follow-ups. |
| `AgenticAPIBrain` | `agents/brain.py` | PSA Intelligence / goal runs when `AGENTIC_API_ENDPOINT` set | Tool-calling loop against an OpenAI-compatible endpoint (currently Ollama). |
| `ExceptionBrain` | `agents/exception.py` | "Ask the agent" / Attention needed | Ranks findings + recommendations. |

Selection is config-driven: `default_brain()` (planner/LLM) and
`default_intel_brain()` (rule-intel/LLM) return the LLM the moment
`AGENTIC_API_ENDPOINT` is set — nothing else changes.

### 3.3 The tool registry & permission levels

18 tools total; **16 are visible to the LLM**, 2 are rule-planner-only:

- **Read tools** (always allowed, traced): `list_vessels`, `list_containers`,
  `list_shipments`, `list_vessel_stowage`, `get_mcc_plans`,
  `get_outbound_containers`, `get_bookings`, `get_yard_status`, `get_drayage`,
  `get_slas`, `get_psch_space`, `get_terminal_snapshot`, `get_trace_events`.
- **Mutate tools (granular, LLM-visible):** `reassign_bin` (one container's
  `bin_location`), `reschedule_receiving_area` (one `receiving_area`),
  `release_lane` (one outbound `lane_release_time`).
- **Hidden from the LLM (`expose_to_llm=False`):** `save_mcc_plans`,
  `save_outbound_containers` — the whole-batch writers. The planner uses them;
  the LLM physically cannot.

Every tool has a name, description, JSON parameter schema, and a permission
level (`read` / `mutate` / `approval`). `call_tool` is the single funnel: it
executes the handler **and records the `tool_call` trace event**.

### 3.4 Autonomy levels

`AGENTIC_AUTONOMY` (default **advisory**):
- **advisory** — agent proposes; nothing executes without your review.
- **semi_autonomous** — low-risk `mutate` auto-applies; `approval` still needs a human.
- **autonomous** — everything auto-applies (demo mode only).

The runtime's `gate()` returns `{status: "pending_approval"}` for gated tools;
`approve()` executes them, `reject()` records the decision and executes nothing.

### 3.5 The human-in-the-loop approval flow

1. You ask "move MAEU4801288 to bin 5-08-1B" (chat or toolbar).
2. The brain returns a `pending_approval` event; the runtime records
   `approval_required` (agent) in the trace; the page renders **Approve / Reject**
   buttons under the answer.
3. **Approve** → `approved` (runtime) trace event → the tool executes
   (`tool_call`) → the plan row updates → the Storage page shows the new bin →
   the reasoning line is appended.
4. **Reject** → `rejected` (operator) trace event → nothing changes.

The AI Changes trace view shows this exact chain with PENDING / APPROVED /
REJECTED badges. The LLM brain **never executes** mutate/approval tools inside
its loop — it can only propose; the runtime/human applies.

### 3.6 The LLM agentic loop (how the "AI" actually decides)

`AgenticAPIBrain.run` builds an OpenAI-style message list:

1. **System prompt** — role, the MCC domain vocabulary, the conversation rule,
   and strict grounding rules (read `get_terminal_snapshot` first for counts,
   never invent numbers, never execute plan changes, never call batch writers).
2. **Conversation history** — the prior thread messages, then the current question.
3. **Tool schemas** — `tool_schemas()` attached for function calling.
4. Loop (bounded by `AGENTIC_MAX_TOOL_ITERATIONS`, default 20):
   - call the endpoint (`_post`, 60s timeout);
   - if the model emits `tool_calls`, execute reads directly or queue
     mutate/approval as `pending_approval`, feed results back as `tool` messages;
   - when the model stops calling tools, its final message is the answer.
5. The whole run is recorded (`agent_run_start` with goal/brain/autonomy/history
   length, `agent_run_end` with the summary).

### 3.7 Conversational memory

- The thread (`localStorage` key `psa-intel-thread`) is shared by PSA
  Intelligence and the toolbar goal bar.
- Every question sends the **last ~10 messages** as history (the server keeps up
  to 12). The LLM sees them as chat context; the rule brain walks the history
  newest-first to resolve pronouns ("and what about its vessel?" → the
  last-mentioned container's carrying vessel).
- Word-bounded reference detection means "utilisation" never trips the "it" check.

### 3.8 The exception agent

- `scan_exceptions(plans, containers, outbounds, vessels, now)` — pure function,
  used by the 8s poll (never traces, so it doesn't flood the audit trail).
- `find_exceptions(path)` — the traced variant for agent runs.
- Detects five kinds: **receipt_eta_missed** (critical if very late), **customs_hold**,
  **loading_window_missed**, **loading_cutoff_approaching**, **vessel_etd_slip**.
- `ExceptionBrain.run` formats them into a ranked answer with recommendations
  (e.g. "Re-sequence the receiving plan (reschedule_receiving_area) or expedite
  the road leg").

### 3.9 Config & the Ollama setup

In `.env` (or environment):

```
AGENTIC_API_ENDPOINT=http://localhost:11434/v1/chat/completions
AGENTIC_API_KEY=ollama
AGENTIC_API_MODEL=qwen2.5:7b
```

- Unset the endpoint → the rule brain answers instantly, zero cost.
- The endpoint is OpenAI-compatible, so any provider with function calling
  (Ollama, Groq, Gemini, OpenRouter, a PSA-internal API) plugs in via config.

### 3.10 Where the AI shows up in the UI

PSA Intelligence chat (answers + Approve/Reject) · toolbar goal bar (same
thread) · Attention needed on the Control Tower ("Ask the agent") ·
Execution Trace (AI Changes view) · the trace's approval lifecycle badges ·
Storage/Outbound pages reflecting approved changes on the next 8s poll.

### 3.11 How plans are generated, and how the agent takes action

**Plan generation is deterministic and rule-driven, not LLM-generated.**
Every cargo plan you see is computed by `plan()` in `agents/mcc_planner.py`
from the seeded synthetic world (400 containers, 5 flows, vessels, roads). It
walks each container through the real logistics chain — vessel ETA → unload →
port depot → road → PSCH receipt → staging → putaway → pallet pick →
consolidation/release — and applies fixed business rules at each step:

- **Journey timing:** derived from the carrying vessel's ETA and fixed leg
durations (unload, depot, road), with the road-delay layer (≈12% of
containers, 1–8 h) slipping the receipt ETA.
- **Receiving:** doors opened proportionally to the inbound arrival rate over
the next 6 h; each container is assigned a door by rotation.
- **Slotting/putaway:** dwell-based height — cargo released soon sits at floor
level for fast robot retrieval, slow movers go higher; reefer → cold room,
hazmat → the segregated aisle, everything else ambient.
- **Whole-container flows (FCL / Top Up / Transload):** staged in yard
slots/bays and released whole, never deconsolidated into rack bins.
- **Outbound consolidation (MCC):** cargo grouped by destination into
40-foot units, each bound to a specific vessel, with stuffing windows, loading
lanes, and a loading cell on the vessel's bay plan.

Every decision writes an **explicit `reasoning` string** (now stored as
structured, newline-separated `Label: value` lines — Journey / Timeline /
Receiving / Flow / Plan / Staging — rendered as tidy labelled rows in the
Agent reasoning panels instead of a wall of text).

**The agent layer acts on top of those plans, granularly and never silently.**
The plan is written through the tool registry as *proposals* (`reassign_bin`
per container, `reschedule_receiving_area`, `release_lane` — one plan field
per call), so:

1. A goal/question reaches the brain (rule or LLM) through the runtime.
2. The brain decides what to change and emits one or more **tool calls**.
3. `call_tool` applies a permission gate: `approval`-level tools are **not**
executed — they return as *pending proposals* with the exact args.
4. The UI shows an **Approve / Reject** button per proposal, each labelled
with its concrete change (e.g. "Reassign MAEU4801288 → bin 5-08-1B"). When
several proposals come back in one answer (multi-action), each renders as its
own **proposal card** — `Proposal N · tool` header, the change label, and its
own Approve/Reject pair — so no counting of lines is needed to know which
buttons belong to which container.
5. Approval executes the write; rejection records it and changes nothing.
6. Every step is traced (`approval_required → approved/rejected → tool_call`)
in the Execution Trace, and the plan updates on the next 8 s poll.

The design goal is: **deterministic plan + human-approved, granular action**.
The agent never rewrites the plan wholesale — it makes surgical, auditable
edits, one field at a time.

### 3.12 How this compares to industry systems (SAP EWM, Bloomberg Intelligence)

**Short answer: yes — the architecture mirrors them, minus the scale.**

- **SAP EWM** is a warehouse-management platform built on *deterministic*
optimisation engines: allocation, putaway strategies, wave planning, and slotting
rules are computed algorithmically from master data and live stock. "AI" in EWM
means rules/optimisers plus embedded decision services that *flag* exceptions
or propose adjustments for a human planner to confirm. It does not free-form
rewrite your warehouse; it computes plans and supports exception-driven action.
- **Bloomberg Intelligence / the terminal** is deterministic on the data side —
thousands of computed indicators, models, and analytics derived by fixed code
— and uses ML/LLMs as an *assistant layer* that reads that data, answers in
natural language, and surfaces alerts. The numbers are computed; the language
around them is generated.
- **This software** is the same shape: a deterministic core (`mcc_planner`
computes every plan and KPI), a rule brain that explains those plans and
proposes changes, an optional LLM that does the same in natural language, and
an agentic action layer that proposes granular plan changes which a human
approves. The one honest difference: EWM/Bloomberg optimise over millions of
units with proprietary solvers; here the "solver" is a compact rule engine
over 400 synthetic containers. The *pattern* — compute deterministically,
flag exceptions, act only with approval — is exactly the industry one.

---

## Chapter 4 — Honest limitations & workarounds

Facts, no sugar-coating. Each item says what to expect and how to work around it.

### 4.1 The sim clock resets every server restart

`sim_now()` is **process-anchored**: `SIM_NOW + (seconds since boot) × 60`.
Stop and restart the server hours later, and the world replays from the seed
moment — journey stages, stacker charges, lanes, exceptions all restart.
The DB persists, but since every status is *derived* from the clock, the
terrarium effectively restarts.
**Workaround:** record a demo/video in one uninterrupted session; for a live
judge demo, restarting just before you present is fine (the world looks the
same deterministic seed). True continuity needs a persisted clock offset —
not built yet.

### 4.2 The LLM can hallucinate or guess

`qwen2.5:7b` is a small local model. It *will* occasionally invent numbers,
mis-state facts, or describe things it never read. Mitigations already live:
the `get_terminal_snapshot` tool + strict grounding prompt, and the rule brain
for deterministic answers.
**Workaround:** for any hallucination-prone segment (counts, block-level stats),
either re-ask (probabilistic) or **unset `AGENTIC_API_ENDPOINT`** so the rule
brain answers — identical interface, zero hallucination risk, instant.

### 4.3 The LLM can narrate a proposal without calling the tool

Occasionally the model *writes* "I have proposed moving X…" in prose but never
emits the `reassign_bin` tool call — so **no Approve/Reject buttons appear** and
the trace shows no `approval_required`. This is a model behaviour, not a code bug.
**Workaround:** the rule brain always produces a real proposal; for demos of the
approval flow, prefer it (or re-ask the LLM). The trace makes this visible: a
run with no approval event means nothing was actually proposed.

### 4.4 LLM latency

First call after a server restart is **30–60s** (model loads into RAM). After
that, replies are faster but still seconds.
**Workaround:** warm the model with one question before the demo; consider
`qwen2.5:3b` for snappier replies; or use the rule brain for instant answers.

### 4.5 Rule brain limits

`IntelRuleBrain` is keyword-intent matching. It is deterministic and free but:
- vague multi-intent questions can fall to the fallback;
- pronoun follow-ups only resolve to the **last-mentioned container** in the
  thread window (not arbitrary earlier references);
- it cannot do arithmetic or cross-fact synthesis beyond its canned builders
  (counts, flows, vessels, warehouse, outbound, exceptions, recent, help).
**Workaround:** phrase questions with a container ID or a direct keyword; for
complex reasoning use the LLM brain.

### 4.6 Memory is per-browser and windowed

Conversation persistence is **localStorage only** (per browser profile) and the
context window is ~10–12 messages. A different browser/machine won't see the
thread, and very long chats lose the oldest context.
**Workaround:** keep demo conversations short; there is no server-side thread
store yet (a listed future step).

### 4.7 Cloud deployment caveats

- **Serverless (Vercel etc.) cannot run this** — it is a long-running, stateful,
  threaded server with a file DB; not a rewrite-free fit.
- **Free long-running hosts (Render free, Fly.io free allowance, HF Spaces) work
  but sleep after inactivity** — and because of 4.1, every wake resets the sim.
- **Ollama is local** — a cloud host cannot reach `localhost:11434`; you would
  point `AGENTIC_API_ENDPOINT` at a hosted OpenAI-compatible API (Groq/Gemini
  free tiers support function calling) — config change, no code change.
**Workaround:** commit to GitHub; record the video locally (reliable); optionally
stand up a Render free instance with a hosted LLM for a clickable live link.

### 4.8 No bulk/multi-container changes through the AI

The granular tools are deliberately one-container-one-field. The LLM cannot
batch-move, re-slot a whole aisle, or edit the plan set (batch writers are
hidden). This is a **safety feature** (the "not rewrite the codebase" guarantee),
not an omission.
**Workaround:** ask for one change at a time; complex re-plans are the
planner's job (Run Hub Planner).

### 4.9 Receiving-lane details are disabled

Receiving lanes are viewable (with flashing) but **not clickable** for container
detail — decided after a UX iteration; details are available via the left list.

### 4.10 Trace timestamps are sim time

The trace deliberately uses `sim_now()`, not the wall clock. Do not "fix" it
back to real time — the whole world is sim-time based.

### 4.11 Everything is synthetic and simplified

The domain model captures the *shape* of port/CFS operations (stages, lanes,
stackers, QC, exceptions) but abstracts real-world complexity: crane scheduling,
truck routing, physical stacking constraints, customs paperwork, human labour.
Some numbers are approximations by design (e.g., QC tasks are scheduled windows;
yard utilisation is a block-level figure). It is a demo/teaching terrarium, not
a production system.

---

## Chapter 5 — Quick reference

### Run it

```bash
.venv/Scripts/python.exe server.py        # http://127.0.0.1:8513
.venv/Scripts/python.exe -m streamlit run dashboard/app.py   # alternative UI
```

### Key config (env or .env)

`SIM_SPEED` (60) · `SIM_SEED` (42) · `N_CONTAINERS` (400) · `DRAYAGE_TOTAL` (12) ·
`PSCH_RECEIVING_LANES` (10) · `PSCH_RELEASING_LANES` (40) · `PUTAWAY_ROBOTS` (8) ·
`AGENTIC_API_ENDPOINT/KEY/MODEL` · `AGENTIC_AUTONOMY` (advisory) ·
`AGENTIC_MAX_TOOL_ITERATIONS` (20).

### HTTP API (classic UI)

`GET /api/state`, `/api/health` · `POST /api/seed`, `/api/agent/plan`,
`/api/agent/run`, `/api/agent/exceptions`, `/api/agent/approve`,
`/api/agent/reject`, `/api/intel/ask`, `/api/bayplan`, `/api/berth/inspect`,
`/api/psch/inspect`, `/api/trace/clear`.

### Key files

```
server.py               the whole classic UI (HTML/CSS/JS) + HTTP handlers
config.py               sim clock, facility layout, agentic config
data/store.py           SQLite layer (incl. granular plan update functions)
data/simulator.py       seeded synthetic world generator
data/facility.py        PSCH racks/lanes/AS/RS derivation
agents/mcc_planner.py   the deterministic planning brain (+ road delays)
agents/brain.py         AgentBrain protocol, RuleBasedBrain, AgenticAPIBrain
agents/intel.py         IntelRuleBrain (chat Q&A)
agents/exception.py     exception scanner + ExceptionBrain
agents/runtime.py       runtime, gates, approvals, trace recording
agents/tools.py         the tool registry (18 tools)
analysis/kpis.py        authoritative KPIs (get_terminal_snapshot)
analysis/bayplan.py     Bay-Row-Tier stowage visualisation
tests/                 92 tests (pytest)
```

### Tests

```bash
.venv/Scripts/python.exe -m pytest -q    # 92 passing
```

### The AI layer at a glance

4 brains · 18 tools (16 LLM-visible) · 3 permission levels · 3 autonomy levels ·
1 funnel (`call_tool`) · 1 audit trail (trace, sim time) · human-in-the-loop
approvals · ~10–12 message conversation memory · rule brain free/deterministic,
LLM brain via any OpenAI-compatible endpoint.
