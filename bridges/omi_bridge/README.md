# omi_bridge

Ingest for an [OMI](https://www.omi.me/) wearable — a **mic-only ambient capture device**, a
portable peer of the Home Assistant Voice PE. It has no speaker, so it only ever *listens*.
`omi_bridge` receives OMI's real-time transcript webhook and writes the utterances into the same
isolated `ambient.db` the [`ambient_bridge`](../ambient_bridge/) uses — **Stage 1: capture only.**
It never contacts Genesis.

Two ambient devices, **one substrate**: PE rows are tagged `source=ambient-<ip>`, OMI rows
`source=omi-<uid>`; both share the `ambient_overheard` provenance so a future edge attention
engine scans them together. The device is distinguished by `source`, nothing else.

## How it works

```
OMI phone app ──(real-time transcript webhook, HTTPS)──▶ Tailscale Funnel ──▶ omi_bridge ──▶ ambient.db
```

OMI posts small JSON batches (often a single segment) as you speak. `omi_bridge`:

1. **Authenticates** on a secret path token (`/omi/<token>/ingest`) + an optional uid allowlist.
   OMI's dev webhook can't send custom headers, so the secret rides the URL.
2. **Dedups** by the per-segment UUID `id` (OMI resends/refines segments); an `Idempotency-Key`
   header is honored too when present.
3. **Anchors** OMI's conversation-relative `start`/`end` seconds to wall-clock time (self-correcting
   across conversation gaps).
4. **Writes** to `ambient.db` via the shared `AmbientStore` (no `audio` block → text-only path;
   OMI's per-segment `is_user` is recorded via `set_identity`).

It is a deliberately quiet webhook citizen: after auth it **never returns 5xx** (a drop is safer
than tripping OMI's retry / circuit-breaker / auto-disable machinery) and **never returns a
`message` field** (OMI would turn one into a phone push notification).

## Configuration (env)

| Var | Default | Notes |
| --- | --- | --- |
| `OMI_INGEST_SECRET_TOKEN` | — (required) | Long random secret; rides the webhook URL path. |
| `OMI_INGEST_SECRET_TOKEN_PREVIOUS` | — | Set during rotation so the old token keeps working. |
| `OMI_UID_ALLOWLIST` | *(empty = open)* | Comma-separated OMI account uid(s). Second factor. |
| `OMI_HTTP_HOST` / `OMI_HTTP_PORT` | `127.0.0.1` / `8788` | Loopback: only Funnel should reach it. |
| `OMI_DB` | `~/ambient.db` | **Shared** with the ambient bridge, by design. |
| `OMI_STATE_DB` | `~/omi_state.db` | Isolated dedup/anchor state (never `ambient.db`). |
| `OMI_TTL_HOURS` / `OMI_ROW_CEILING` | `48` / `200000` | Rolling quarantine (same as ambient). |
| `OMI_ANCHOR_TOLERANCE_S` | `60` | Re-anchor when the predicted time drifts past this. |

See [`../../deploy/omi.env.example`](../../deploy/omi.env.example).

## Deploy

```bash
deploy/install.sh omi                 # creates ~/omi-venv (aiohttp), stages the systemd unit
mkdir -p ~/.omi && cp deploy/omi.env.example ~/.omi/omi.env   # then fill in the token + uid
systemctl --user daemon-reload && systemctl --user enable --now omi-bridge
```

Then expose it with Tailscale Funnel and point the OMI app's **Real-Time Transcript Webhook**
(Settings → Developer Mode) at `https://<your-funnel-host>/omi/<token>/ingest`. Wire protocol and
the "one public door" rationale are in [`../../CONTRACTS.md`](../../CONTRACTS.md).

## Not built yet (Stage 1 is capture-only)

Diarization, the attention/filter tiers, and any graduation of OMI signal into Genesis are future
work. When the attention engine goes live it will scan `ambient.db` device-agnostically; the
per-device difference is only what happens *after* a perk-up — the PE can chime in verbally, OMI
(no speaker) can only trigger background work + a Telegram ping (and, eventually, a physical
"perk-up" button — the analogue of the Voice PE's center-button `/marker`).
