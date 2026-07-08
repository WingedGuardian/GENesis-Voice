# Omnipresence roadmap — from ambient capture to proactive action

**Status: design notes, not built.** Stage 1 (capture) is what ships in the repo today: the Voice
PE and the OMI wearable both write overheard speech into an isolated, short-lived `ambient.db` on
the edge, and nothing reaches Genesis. This doc records where that goes and the decisions already
made, so later passes don't relitigate them. Each phase below is its own separate design + build.

## The shape of it

Two ambient devices, **one substrate, one engine**:

- The **Voice PE** streams raw audio → local STT → `ambient.db` (`source=ambient-<ip>`).
- The **OMI wearable** (mic-only, no speaker) posts cloud-STT'd transcript → `ambient.db`
  (`source=omi-<uid>`).

They are peers. A single attention engine scans `ambient.db` **device-agnostically**; the only
per-device difference is what happens *after* it perks up (see Phase 3). Device identity lives in
`source`; both share `provenance=ambient_overheard` so one query sees everything.

## Guiding constraint

> Acting on a mis-hear is worse than silence.

Raw ambient STT is noisy. Every phase past capture is gated on precision, not built for its own
sake. The firehose stays quarantined on the edge; only small, explicit, `is_user`-gated **perk-up
events** ever cross into Genesis — never raw transcript text.

## Phase 1 — Edge attention loop (shadow)

Run the (already edge-portable) L1 attention gate live on the edge, over `ambient.db`, scanning
both devices. **Shadow only** — it decides *would I perk up here?* and logs it; nothing acts. Tune
the perk-up threshold on real OMI + PE speech. Recommendation from planning: the engine runs on the
**edge** (co-located with the raw data it quarantines), and only its verdicts graduate.

## Phase 2 — Graduation boundary

A narrow, one-way, `is_user`-gated channel that sends **perk-up events (not raw text)** edge →
Genesis, plus a live attention consumer on the Genesis side. Still notify-only; no autonomous
action yet. This is the boundary deliberately left undefined in `CONTRACTS.md §3` — its design is
this phase's job.

## Phase 3 — Confirm + actuation (the payoff)

A confirmed perk-up drives real work. The **per-device asymmetry** lives here, downstream of the
shared engine:

- **Voice PE** (has a speaker): can chime in verbally via the s2s path — "want me to start on that?"
- **OMI** (no speaker): cannot speak, so a perk-up drives **background work + a Telegram ping**.
  The vision: overhear "we need XYZ built", and Genesis has a draft ready before the conversation
  ends, with a Telegram note. A physical **perk-up button** on the device (the analogue of the Voice
  PE center-button `/marker`) can force attention on demand.

Actuation routes through the existing autonomy discipline: owner-facing delivery (Telegram) is
ungated; any external action stays behind the capability gate; autonomous *building* uses the
draft-PR (never auto-merge) lane.

### Signal sources to weigh here (not only the raw firehose)

- **OMI `memory_created` webhook** — fires at conversation end with a structured summary and
  device-side **`action_items`** already extracted. A lower-noise "build XYZ" trigger than the raw
  real-time stream (post-hoc, but high precision).
- **OMI real-time `message` response** — returning a `message` in the webhook 200 makes OMI push a
  phone notification. A ready-made "ping me" actuation lever (deliberately unused in Stage 1).

### OMI MCP / Developer API — scoped to the attention engine, NOT a Genesis tool

OMI exposes an MCP server + Developer API (query your own memories/conversations). This is **not**
wired as a general foreground Genesis tool. If used, it is a **capability of the attention engine**:
at perk-up time the engine may pull OMI's *cleaned* transcript + `action_items` to **confirm** a
candidate before acting (raising precision against the mis-hear gate). Constraints:

- the OMI API key lives **with the engine** (edge-side), not in Genesis's shared secrets;
- it is **never registered** in the MCP tool registry any foreground session loads;
- whatever it pulls stays **behind the graduation boundary** — only perk-up events cross, never raw
  OMI memories.

It is post-hoc by nature (OMI memories exist only after a conversation ends), so it enriches
*follow-through*; the real-time reflex still rides the webhook + the engine's own judgment.
