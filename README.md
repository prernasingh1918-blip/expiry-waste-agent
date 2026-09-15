# Kitchen Expiry & Waste Reduction Agent

An AI agent that reviews a restaurant's raw ingredient and prepped-item stock,
reasons over a food-safety policy document, and decides — per item — whether
to leave it, flag it for use, convert it to prep, write it off, or escalate
for human review. Built as a genuinely agentic system: the AI chooses which
tools to call and in what order, rather than following one fixed script.

**Domain:** Restaurant/food-service supply, Indian cuisine
**Core techniques:** Tool-calling agent loop, retrieval-augmented grounding (RAG), deterministic verification baseline

---

## The Problem

Restaurants lose money to ingredient spoilage and prep waste that often goes
untracked — not because staff don't care, but because monitoring dozens of
ingredients and prepped items against shifting use-by windows and usage
rates is tedious and easy to miss in a busy kitchen. This project builds an
AI agent that does that monitoring continuously and explains its reasoning.

## Why This Is a Genuine Agent, Not Just Automation

The agent is given a **goal** ("assess this batch") and a small set of
**tools** — it decides for itself which tools to call, in what order, and
how many steps it needs, based on what each tool returns. A clean batch
might resolve in two tool calls; an ambiguous one might take four. Nothing
about the sequence is hardcoded.

**Tools available to the agent:**
| Tool | Purpose |
|---|---|
| `get_days_remaining` | Days left until a batch's use-by date |
| `get_usage_pace` | How usage compares to elapsed shelf-life time |
| `get_policy_for_batch` | Retrieves the relevant food-safety policy section for that batch's category (RAG) |
| `get_prep_status` | Timing/usage status for a prepped item |
| `get_prep_source_check` | Traces a prepped item back to its raw source batch(es) and their status at prep time |
| `get_prep_policy` | Retrieves the relevant policy for a prepped item |

## Retrieval-Augmented Grounding (RAG)

The agent's decisions are grounded in a real policy document
(`kitchen_waste_policy.md`) — a synthetic kitchen SOP covering category-specific
rules (seafood, dairy, produce, meat & poultry, dry goods, bakery). Rather
than hardcoding thresholds into the agent's prompt, the agent retrieves the
relevant policy section by category and reasons over the actual retrieved
text — verified by checking that its stated reasoning quotes real policy
language, not invented rules.

## A Genuine Safety-Critical Decision: The Override Rule

The most important design decision in this project: if a prepped item was
made from a raw ingredient batch that had **already expired** at the time of
prep, the agent overrides its normal timing-based assessment and escalates
to `URGENT_REVIEW` — regardless of how "fine" the prep item looks by its own
use-by window. This was found and fixed during testing, not designed in
from the start (see below).

## Real Bugs Found & Fixed During Development

Building this surfaced genuine failure modes worth documenting honestly:

1. **Silent grounding failure** — a naive retry wrapper resent the original
   prompt on API failure instead of resuming an interrupted tool call,
   causing the agent to answer without ever reading the real retrieved
   policy text. Fixed by retrying with a fresh session instead.
2. **Cross-step state loss** — a smaller/faster model correctly identified
   a batch's category in step 1, then passed the *wrong* category to a
   later tool call. Fixed at the tool-design level: the tool now derives
   category internally instead of relying on the model to carry it forward.
3. **Action-taxonomy ambiguity** — the agent conflated a generic "at risk"
   reading with a more specific "policy recommends conversion" reading.
   Fixed by explicitly defining each action label's meaning in the prompt.
4. **Data type regression** — reloading data without re-applying date
   conversion silently turned dates back into plain text, breaking date
   math inside a live tool call. Caught via direct type inspection.

Each was diagnosed using `chat.get_history()` to inspect the actual
tool-calls-and-responses exchanged with the model — not guessed from the
final answer alone.

## Deterministic Baseline (Verification Layer)

Alongside the LLM agent, a deterministic Python classifier encodes the same
policy rules directly. This isn't a shortcut — it's the same verification
pattern used in my prior project (Vantage): the deterministic
layer provides ground truth to test the agent against, and powers the bulk
statistics below without spending API quota on every one of 66 items.

## Results

Run across a synthetic single-restaurant dataset (44 raw ingredient batches,
22 prepped items, Indian cuisine):

| Raw Batch Action | Count |
|---|---|
| ON_TRACK | 21 |
| AT_RISK | 13 |
| WATCH (early warning) | 4 |
| CONVERT_TO_PREP | 3 |
| ASK (ambiguous) | 2 |
| WRITE_OFF | 1 |

| Prepped Item Action | Count |
|---|---|
| ON_TRACK | 16 |
| AT_RISK | 5 |
| URGENT_REVIEW (safety override) | 1 |

**Roughly half of active stock needed some form of attention**, worth an
estimated **₹19,925** in at-risk/written-off raw ingredient value alone —
and one prepped item was flagged for urgent food-safety review that a
simple timing-only check would have missed entirely.

## Tech Stack

- **Python** (pandas) — data modeling, deterministic classifiers, tool functions
- **Google Gemini API** (`gemini-3.6-flash` / `gemini-3.5-flash-lite`, free tier) — agent reasoning and tool-calling
- **Google Colab** — development environment
- Synthetic dataset generated for a single-restaurant Indian kitchen, with
  deliberately planted edge cases to stress-test every decision branch

## What's Next

- Upgrade retrieval from category-keyword lookup to embedding-based
  semantic search
- Add short-term memory so the agent can reference past decisions on
  similar batches
- Expand to a small restaurant chain (multi-location redistribution
  decisions)
