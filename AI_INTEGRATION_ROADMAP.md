# AI Integration Roadmap — from PSA Intelligence page to live agentic behaviour

> **Status:** Phases 0–4 are implemented and live (Phase 2 absorbed into the
> Ollama wiring — see below). Phase 5 (live-data polish + video) is the only
> remaining work. Each phase lists the exact files, endpoints, tests and
> verification steps; every phase below marked ✅ is running in the current
> codebase and verified live on port 8513.
>
> Design contract: `AGENTIC_AI_ARCHITECTURE.md` · Integration map:
> `AGENTIC_AI_INTEGRATION_MAP.md` · Agent onboarding: `AGENT_ONBOARDING.md`.

## The one idea that keeps every phase small

Everything already routes through one seam:

```
user question / goal
        │
        ▼
   /api/intel/ask ──► AgentRuntime.run(goal, context)
                          │  AgentBrain
                          │    ├─ RuleBasedBrain     (deterministic planner)
                          │    ├─ IntelRuleBrain     (deterministic Q&A — Phase 0)
                          │    └─ AgenticAPIBrain    (external LLM, function calling)
                          ▼
                   agents/tools.py: call_tool(name, args)
                          │
                          ▼
                   data/store.py (SQLite)  +  execution trace
```

Swapping the brain is a **config change** (`AGENTIC_API_ENDPOINT` in `.env`).
The page, the endpoint, the tools and the trace never change. That is why
Phases 1–5 are mostly *wiring and proving*, not rewriting.

---

## Phase 0 — PSA Intelligence page + rule-mode brain  ✅ DONE

**Goal:** a prompting page ("ask anything, literally anything") that answers
from the live data deterministically today, and is built so the LLM brain drops
in behind the same endpoint tomorrow.

| Piece | File | What |
|---|---|---|
| Page | `server.py` | `#view-intel` dark Gemini-style panel + nav item "PSA Intelligence" |
| Endpoint | `server.py` | `POST /api/intel/ask` → `AgentRuntime(brain=default_intel_brain()).run(question, {})` → `{ok, answer, brain, autonomy}` |
| Rule brain | `agents/intel.py` | `IntelRuleBrain` — intent detection over the tool registry (container lookup, KPI counts, vessel, flow, "why", warehouse state, help) |
| Brain picker | `agents/intel.py` | `default_intel_brain()` → `AgenticAPIBrain` if `AGENTIC_API_ENDPOINT` set, else `IntelRuleBrain` |
| New tools | `agents/tools.py` | `get_psch_space`, `get_trace_events` (read-level; the LLM brain will use them too) |
| Tests | `tests/test_intel.py` | container lookup, KPI counts, "why", fallback help, brain fallback |

**Verify:** open the page, ask "where is SEAU9342928", "how many containers are
at sea", "what is the bin utilisation", "why is … in bin 1-12-2A" — each returns
a live-data answer; footer shows `rule-based-intel-v1 · advisory`; each answer
lands in the Execution Trace as `agent_run_start`/`agent_run_end`.

---

## Phase 1 — the goal endpoint: `/api/agent/run` (the agentic "Run" button)  ✅ DONE

**Goal:** let the user (or the demo) send an arbitrary *goal* through the same
runtime — the thing the brief calls "an agent that acts on the world".

**Files:**
- `server.py` — add:
  ```
  POST /api/agent/run  {"goal": "...", "context": {...}}
  ```
  Handler: `rt = AgentRuntime(); result = rt.run(goal, context)`; return
  `{ok, summary, error, events, brain, autonomy}`. This is *exactly* the
  `AgenticAPIBrain` loop when the endpoint is configured — no new machinery.
- `server.py` (JS) — toolbar button **"▶ Run Agent Goal"** next to the existing
  buttons; a small modal/inline box to type a goal; render `result.summary`
  into the Execution Trace page (or a new "Agent" panel).
- `tests/test_server.py` — POST the endpoint with the rule brain; assert the
  result is a `BrainResult` shape and trace events were recorded.

**Verify:** click the button, type `"Plan MCC consolidation for the next 24h"`,
watch the trace fill with `agent_run_start` → `tool_call` (list_vessels,
list_containers, …) → `save_mcc_plans` → `agent_run_end`. With the rule brain
this is deterministic; with `AGENTIC_API_ENDPOINT` set it is the LLM doing the
same tool calls.

**Why first:** it proves the runtime loop end to end *before* any LLM is
involved, and gives the demo a "send a goal → agent acts" moment even offline.

---

## Phase 2 — activate the LLM brain (the config flip)  ✅ DONE (Ollama wired)

**Goal:** point `AGENTIC_API_ENDPOINT` at a real OpenAI-compatible endpoint and
see the *same* `/api/agent/run` and `/api/intel/ask` answered by the LLM.

**Files:**
- `.env` — set `AGENTIC_API_ENDPOINT`, `AGENTIC_API_KEY`, `AGENTIC_API_MODEL`
  (e.g. `https://api.openai.com/v1/chat/completions`, `gpt-4o-mini`).
- `agents/brain.py` — *only if the provider's response shape differs*. The
  reference loop expects `choices[0].message.tool_calls`; adapt `_post()` /
  parsing if your provider differs (Azure, Groq, OpenRouter, Ollama all use the
  OpenAI shape; a local Ollama server costs nothing).
- `agents/brain.py` — tune `_system_prompt()` so the model answers the *page's*
  questions: add "You answer user questions about live terminal state by calling
  tools; cite the container/vessel/plan you read; keep answers concise."
- `tests/test_brain.py` — mock `_post()` with a canned tool-call + final-message
  sequence; assert the loop executes tools and returns the final summary (no
  network needed — this is the regression test that keeps the seam honest).

**Verify:** with the endpoint set, ask the page "which containers are delayed
past their receipt ETA?" — the trace shows the LLM calling `list_containers` /
`get_mcc_plans`, then a written answer. Unset the endpoint → the page answers
from `IntelRuleBrain` again. No code change either way.

**Cost check:** each question run is a bounded loop (≤20 tool iterations, small
context). At `gpt-4o-mini`-class pricing a full demo is well under $1. For a
zero-cost venue, point the endpoint at a local Ollama server.

---

## Phase 3 — the "system of agents" (exception agent + Q&A = 2 visible agents)  ✅ DONE

**Goal:** the brief wants *agents each serving a purpose*. Two agents, both on
the same runtime, both visible in the demo:

1. **Q&A agent** — this *is* the PSA Intelligence page in LLM mode (Phase 2).
   Purpose: answer anything about the data. Read-only, zero risk.
2. **Exception agent** (integration map D1–D3, A8) — watches the live state for
   dwell overruns, missed cutoffs, customs holds, undercharged stackers, ETA
   slips; proposes a re-plan or an escalation.

**Files:**
- `agents/exception.py` (new) — `scan_exceptions` (pure, untraced — used by the
  8s poll) + `find_exceptions` (traced reads for agent runs) + `ExceptionBrain`
  + `format_exceptions`. Detects receipt ETAs missed (road-delay layer),
  customs holds, outbound loading windows missed/approaching, and vessel ETD
  slips, each with a recommendation.
- `agents/mcc_planner.py` — deterministic road-delay layer (`delay_hours`,
  ~12% of the wave slips 1–8h past its promised receipt ETA) so the synthetic
  world genuinely produces exceptions for the agent to catch; `journey_status`
  uses the delayed arrival so every page stays consistent.
- `server.py` — `POST /api/agent/exceptions` → run `ExceptionBrain`; an
  **Exception Watch** panel on the Control Tower page (populated from
  `build_state` via the pure scan — the 8s poll never floods the trace); an
  **"Ask the exception agent"** shortcut that pre-fills the intel page.
- `tests/test_exception.py` — customs holds, delayed-receipt slips, vessel ETD
  slips, purity (no trace writes), and the traced agent run.

**Verify:** the Control Tower shows live exceptions ("HLCU8382285 — Receipt ETA
missed by 3.8h (delay +8h → …Z), journey 'En Route (Road)'; Recommend:
re-sequence the receiving plan"), each with the agent's reasoning. The demo now
has: planner agent (rules) + exception agent + Q&A agent — three distinct
purposes on one runtime.

---

## Phase 4 — the agent takes action: user preferences change the plan  ✅ DONE

**Goal:** the page doesn't just answer — the user can *ask for a change* and the
agent updates the plan (with human approval, per the autonomy model).

**Files:**
- `agents/tools.py` — granular change tools (the ONLY plan writers the LLM can
  see; the whole-batch `save_mcc_plans` / `save_outbound_containers` are
  `expose_to_llm=False`, rule-planner-only, so the agent can never rewrite the
  plan set):
  - `reassign_bin` (mutate) — "move container X to bin 5-08-1B" → updates only
    that plan's `bin_location` (+ appended reasoning line).
  - `reschedule_receiving_area` (mutate) — move the container to another door.
  - `release_lane` (approval) — sets `lane_release_time=now` so the outbound
    status advances from staged → released; never auto-runs in `advisory`.
  Backed by `store.update_mcc_plan` / `store.update_outbound_container`
  (field-whitelisted in-place UPDATEs — granular by construction).
- `agents/brain.py` — `AgenticAPIBrain` now returns **every** mutate/approval
  tool as `pending_approval` (never executes inside the brain), and the system
  prompt forbids whole-batch rewrites.
- `agents/intel.py` — `IntelRuleBrain._propose_change`: "move X to bin …" /
  "change the receiving area of X to RA-4" / "release the lane of X" return a
  pending proposal in `events`; nothing executes.
- `server.py` — `POST /api/agent/approve` → `AgentRuntime.approve()`;
  `POST /api/agent/reject` (trace-only); the PSA Intelligence page renders
  **Approve / Reject** buttons under any message carrying pending events.
- `tests/test_agent_runtime.py` / `test_intel.py` — granular tool tests
  (one plan changes, others untouched; invalid bin rejected), advisory gating
  → approve applies, LLM-brain gating (mock `_post`, plan unchanged until
  approval), and the rule-brain proposal intents.

**Verify:** ask the page "move SEAU9342928 to bin 5-08-1B" → the answer says
"Proposed … waiting for your approval" → click **Approve** → the Storage page
rack grid shows the container at the new bin (verified live: `MAEU4801288`
moved to `Bin 5-08-1B`) and the reasoning panel explains the change. This is
the *"take action on user preferences"* requirement, with the human-in-the-loop
the brief demands.

---

## Phase 5 — live data in every answer + demo polish

**Goal:** make answers *live* (they already read the live store; make that
explicit) and finish the demo story.

**Files:**
- `server.py` (JS) — the intel page already re-renders the global sim clock on
  the 8s poll; add a "Live at HH:MM:SS (sim …)" line under each answer so the
  judge sees the answer was computed from the current world, and a small
  "tool trace" disclosure under each answer showing the tool calls the brain
  made (from the execution trace) — this is the visible *agentic* proof.
- `README.md` — document the PSA Intelligence page and the phases above.

**Verify:** ask a question, wait a poll, ask again — the numbers differ; the
tool-trace under each answer lists the reads; the video arc from the README
("terrarium running → ask the agent → agent re-plans on approval") is complete.

---

## Suggested order & day-plan (implementation day — mostly done)

| Phase | Status | Outcome |
|---|---|---|
| 0 | ✅ done | PSA Intelligence page + rule-mode brain |
| 1 | ✅ done | `/api/agent/run` + Run Agent Goal button (LLM-verified live) |
| 2 | ✅ done | Ollama + qwen2.5:7b wired; `llama` footer + traced tool calls |
| 3 | ✅ done | Exception agent live on Control Tower (road delays + customs holds + vessel ETD slips) |
| 4 | ✅ done | Approve/Reject flow; plan actually changes from a user request (verified live) |
| 5 | ⏳ next | Live-data polish, tool-trace disclosure under answers, record the video |

## Risks & how this roadmap already mitigates them

1. **API unavailable on demo day** — every phase works with the rule brain;
   the LLM is a config flip. The demo never depends on a network.
2. **LLM latency in a 8s-poll UI** — `/api/intel/ask` and `/api/agent/run` are
   user-triggered, never in the poll path (already the case).
3. **Context budget** — brains read through tools; system prompt caps the
   context snapshot at 4000 chars, tool results at 3000 (already implemented).
4. **Non-determinism in the recorded video** — record with the rule brain for
   the scripted parts and show the LLM live afterwards; or fix goals with
   `SIM_SPEED=0` for near-reproducible replays.
5. **Cost** — bounded loops + cheap model + a free local Ollama option; the
   default brain costs nothing.
