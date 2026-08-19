# DEMO_SCRIPT.md — PSA Control Tower (v2.0.0) — 10-Minute Video Submission

A step-by-step script for a ≤10 minute screen-recording to PSA judges. The
whole app runs locally with `python server.py`; the rule brain works with zero
configuration, and the llama (Ollama) brain is optional but recommended for
the multi-action segment.

---

## Before you record

**Setup (2 minutes, do NOT record this part):**

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows — or source .venv/bin/activate on Mac/Linux
pip install -r requirements.txt

python server.py                  # → http://127.0.0.1:8513
```

Optional (recommended for the multi-action demo, see Segment 5):

```bash
ollama pull qwen2.5:7b            # then set in .env:
# AGENTIC_API_ENDPOINT=http://127.0.0.1:11434/v1/chat/completions
# AGENTIC_API_MODEL=qwen2.5:7b
```

Checklist before hitting record:

- [ ] `python server.py` is running and `http://127.0.0.1:8513` loads.
- [ ] The page shows the header ticker (port ↔ PSCH flow) and a ticking
      **SIM CLOCK** (HH:MM:SS, e.g. `2026-08-16 13:35:15Z`).
- [ ] If you will use the llama brain: `ollama list` shows `qwen2.5:7b` and
      the PSA Intelligence footer reads `brain: llama`.
- [ ] Screen resolution comfortable (the app targets 1024×768+, Netscape-era
      theme is intentional — lean into it).
- [ ] **Fresh world:** delete `data/port.db` before starting so the demo begins
      at the start of a lifecycle (or press **⟳ Regenerate** on camera).

---

## Segment 1 — The terrarium: one complete lifecycle (0:00–2:00)

**What the judges see:** a live, self-running logistics ecosystem — no clicks
required.

1. Point at the **SIM CLOCK** in the top bar. Say: *"Every number in this
   software moves on a simulation clock — synthetic data driving a complete
   port-to-PSCH lifecycle: discharge at Tuas → road to the hub → receiving →
   putaway → storage → consolidation → release back to port."*
2. Let it run for ~20 seconds without touching anything. Call out what changed:
   - the ticker counts (`340 inbound containers · 125 arrived at PSCH …`)
   - the **At sea → Arrived @ PSCH** counters ticking
   - the **Bin util** percentage
   - container cards changing status (`En Route (Sea) → Depot → Arrived`)
3. Open **PSCH ▾ → Inbound**. Say: *"340 containers, each on its own journey —
   vessel, bay-plan cell, customs status, road ETA, receiving plan."*
4. Click one container, e.g. **MAEU4801288**. Point at the **Agent reasoning**
   panel — the labelled rows (`Journey / Timeline / Receiving / Flow /
   Slotting`). Say: *"The planner agent computed this plan before the container
   even arrived — where it receives, where its pallets go in the rack, which
   stacker, when it releases."*
5. (Optional flourish) Press **⛶ Full Screen**, then exit, to show polish.

**Key line:** *"This is not a static dashboard — it is a terrarium: the whole
ecosystem runs on synthetic data through one complete lifecycle, however long
that takes, and regenerates when the wave completes."*

---

## Segment 2 — The rule brain: ask anything, zero cost (2:00–3:30)

**What the judges see:** an AI prompting page that answers from *live data*,
instantly and deterministically — no API key needed.

1. Open **PSA Intelligence**.
2. Type and run:
   - `what is the bin utilisation?` → instant answer with real percentages
     (matches the dashboard).
   - `how many containers are at PSCH and how many are bound for Antwerp?`
     → exact counts (anti-hallucination: the brain reads
     `get_terminal_snapshot`, the same numbers the dashboard renders).
   - `what needs attention?` → the agent lists live exceptions (see Segment 4).
3. Say: *"This is the rule brain — deterministic, zero-cost, always online. It
   answers from the same tool registry the large-language model uses, so the
   two brains are swappable by config and behave identically."*

**Key line:** *"Ask it anything about the data — it can't invent numbers,
because it only answers from the live snapshot."*

---

## Segment 3 — The llama brain + the human-in-the-loop (3:30–5:30)

**What the judges see:** the real agentic seam — an LLM with tools that can
*propose* plan changes but never silently execute them.

1. Confirm the footer shows `brain: llama · autonomy: advisory`.
2. Ask: `move MAEU4801288 to bin 5-08-1B and reschedule OOLU9028993 to RA-4`
   (the LLM call takes ~30–60s on a laptop — keep talking while it works).
3. When the answer lands, point at the **proposal cards**:
   - `Proposal 1 · reassign_bin — Reassign MAEU4801288 → bin 5-08-1B` with its
     own **✓ Approve / ✕ Reject**.
   - `Proposal 2 · reschedule_receiving_area — Reschedule OOLU9028993 → RA-4`
     with its own pair.
   - *"Each change is its own labelled card — you always know which buttons
     belong to which container."*
4. Click **✓ Approve** on both. Say: *"Nothing executes until I approve — the
   agent plans, the human decides."*
5. Open **PSCH ▾ → Storage** and find the container: the new bin / new
   receiving area is now live on the plan, and the **Agent reasoning** panel
   shows an amber `[agent] change` row recording the edit.
6. Open **Execution Trace → AI Changes**. Point at the lifecycle:
   `approval required → approved → tool call`. Say: *"Every decision is
   auditable — proposal, my decision, execution, in sim time."*

**Fallback if the local LLM returns one proposal instead of two** (known 7B
behaviour, see QA suite §F): split into two single questions, or run the same
question again — the important thing is showing *a* proposal card + approve +
trace. Do not promise the model batches 3+ writes.

---

## Segment 4 — Attention needed: the agent flags exceptions (5:30–7:00)

**What the judges see:** the deterministic attention agent scanning live and
offering one-click, agent-gated fixes.

1. Open **Control Tower**. Point at **Attention needed — live findings from
   agent** right under the KPI strip. Say: *"This is a live scan — receipt
   ETAs that slipped (the road-delay layer), customs holds, outbound loading
   windows at risk, vessel ETD slips."*
2. Show the **category chips** with counts (`All · Customs & Compliance ·
   Inbound & Receiving · Outbound & Loading · Vessels & Berths`). Click one to
   filter.
3. Pick a finding with a **▶ Propose** button (e.g. *"Reschedule to the
   least-loaded receiving door"* or *"Release staging lane now"*). Click it →
   the row becomes an inline **✓ Approve / ✕ Reject** pair. Approve it.
4. Open **Execution Trace** and show the finding's fix landing as a traced
   `approval_required → approved → tool_call`.
5. Be honest: *"Detection is deterministic — pre-programmed thresholds over
   live data. What the AI layer adds is the recommendation and the
   human-approved action."* (This matches how SAP EWM flags exceptions —
   deterministic optimisation plus agentic action on top.)

---

## Segment 5 — The system of agents + the seam (7:00–8:30)

**What the judges see:** the architecture behind the demo.

1. Say: *"Four agents on one runtime — `AgentRuntime` + swappable `AgentBrain`:
   the planner (computes every plan), the attention agent (flags exceptions),
   the rule intel brain (instant Q&A), and the llama brain (LLM with tools)."*
2. Show the one pipeline (from SOFTWARE_GUIDE.md Chapter 3):
   `goal → AgentRuntime.run → Brain.run → tool registry → permission gate →
   pending proposal → human approve → traced execution`.
3. Say: *"The LLM can only call granular, whitelisted tools — `reassign_bin`,
   `reschedule_receiving_area`, `release_lane`, one field at a time. The
   whole-batch plan writers are hidden from it. It cannot rewrite the plan
   set — that is the safety guarantee."*
4. (Optional) Show `agents/` in a file browser: `brain.py`, `runtime.py`,
   `tools.py`, `exception.py`, `intel.py` — proof of structure, ~5 seconds.

---

## Segment 6 — Deployment & honesty (8:30–9:30)

1. Say: *"This runs locally with `python server.py` — stdlib HTTP server +
   SQLite, nothing to configure. The rule brain needs no API key, no internet.
   The llama brain runs fully offline via Ollama."*
2. Honest shortfalls (say these — judges respect them):
   - *"The local 7B model sometimes returns one proposal per run instead of
     several — visible in the trace, not a bug."*
   - *"Detection is rule-based; the LLM explains and proposes, it does not
     re-optimise the whole terminal from scratch."*
   - *"Scale: 340 synthetic containers, not millions. The architecture
     (deterministic core + agentic action layer) is the industry pattern —
     the same shape as SAP EWM's optimisers plus decision services."*
3. (Optional) Mention the QA suite: *"317 prompts across facts, boundaries,
   security, multi-action capability — used to test every claim you just saw."*

---

## Segment 7 — Close (9:30–10:00)

1. Press **⟳ Regenerate** or let the wave complete — show the world rebuilding
   itself. Say: *"The lifecycle ends and begins again — synthetic data
   supporting the terminal indefinitely, with an AI layer that plans, flags,
   and acts only with a human's approval."*
2. End card: **PSA Control Tower — V2.0.0** · rule brain + llama brain ·
   human-in-the-loop · 93 tests passing.

---

## Speaker notes — one-liners to reuse

- "The terrarium: one complete lifecycle, running by itself, regenerating
  when the wave completes."
- "The agent plans; the human decides; every decision is traced."
- "Two brains, one tool registry — swappable by config."
- "It cannot invent numbers — it answers from the live snapshot."
- "Granular, whitelisted, gated: the LLM edits one field at a time, never the
  plan set."

## Quick reference

| Thing | Where | What to say |
|---|---|---|
| SIM CLOCK / ticker | top bar | "the world is alive, ticking in sim time" |
| Agent reasoning rows | any container inspector | "planned before arrival" |
| PSA Intelligence | nav | "ask anything — answers from live data" |
| Proposal cards | under an answer with pending changes | "one card per change, its own buttons" |
| AI Changes trace | Execution Trace | "proposal → decision → execution, audited" |
| Attention needed | Control Tower | "live scan + one-click agent-gated fixes" |
| 4 agents | `agents/` | "one runtime, swappable brains" |

## If the demo is judged live (no video)

Same arc, compressed to ~6 minutes: terrarium (1.5 min) → rule-brain Q&A
(1 min) → one multi-action proposal + approve + trace (2 min) → Attention
needed with one approved fix (1 min) → honesty + close (0.5 min). Have the
browser pre-loaded on the Storage page so a container is one click away.
