# Contracts

The interfaces between the three boxes. These are the stable surfaces; everything else is
an implementation detail.

## 1. Device to edge — audio WebSocket

The Voice PE firmware opens a WebSocket to a bridge and streams raw audio. Both bridges
speak the same wire contract. The firmware maintains up to two such sockets:

- **Conversational (s2s)** — opened on wake word to `server_url`. Binary frames (opcode
  0x02) of raw **16-bit little-endian, mono PCM at 24 kHz**, ~4800 bytes per 100 ms (the
  firmware upsamples the 16 kHz mic for the conversational model).
- **Ambient** — opened when the **Ambient Mode** switch is ON, to `ambient_url`. Same frame
  format and opcode, but **16 kHz mono** (~3200 bytes per 100 ms) — the native mic rate,
  sent without upsampling. Streamed only while no conversation is active (conversation
  wins). The ambient bridge must run with **`AMBIENT_INPUT_SR=16000`** so its resample is a
  no-op. (The bridge still accepts 24 kHz from a non-ambient sender and downsamples; the
  rate is the sender's choice, declared via `AMBIENT_INPUT_SR`.)
- **Control frames** — text WebSocket frames carrying JSON: `{"type": "interrupt"}` to
  cut off playback, `{"type": "disconnect"}` to end the session. The device treats a *bare*
  socket close as a dropped connection and auto-reconnects — so to end a session the bridge
  **must send `{"type": "disconnect"}` and then close** (see `s2s_bridge` `ws_control`).
  Closing without it orphans the device: it reconnects into a torn-down session and spins
  forever. (Ambient is one-way; the ambient bridge sends no control frames back.)
- **No auth / no handshake.** The socket is expected to live on a trusted local network.
  Do not expose a bridge port to the public internet.
- Keepalive is standard WebSocket ping/pong.

## 1b. Device (via OMI cloud) to edge — OMI transcript webhook

An [OMI](https://www.omi.me/) wearable is a second, mic-only ambient device. Unlike the Voice
PE (raw audio over a LAN WebSocket), OMI transcribes in its own cloud and **POSTs text segments
to an HTTP webhook over the public internet**. So this is the one place the edge accepts an
inbound public connection — the deliberate, authenticated exception to §1's "do not expose a
bridge port to the public internet". It is terminated by **Tailscale Funnel** on the edge and
forwarded to `omi_bridge` on loopback; there is no LAN alternative (OMI's cloud is the sender).

- **Endpoint** — `POST /omi/<token>/ingest?uid=<uid>`. Body is a JSON object
  `{"segments": [ ... ], "session_id": "<uid>"}` where `session_id` **equals** the account
  `uid` (also on the query). Each segment: `id` (stable UUID — the dedup key), `text`,
  `speaker` (e.g. `"SPEAKER_1"`), `speaker_id`, `is_user`, `person_id`, `start`/`end`
  (**conversation-relative seconds**), plus `translations` / `speech_profile_processed` /
  `stt_provider`.
- **Auth** — a secret token carried in the **URL path** (`<token>`), optionally gated by a uid
  allowlist. OMI's dev webhook cannot send custom headers, so the secret cannot ride a header.
  A `…_PREVIOUS` token is honored during rotation.
- **Response discipline** — after auth the receiver returns **only 2xx** (a drop returns 200):
  a non-2xx makes OMI retry (1s/5s/30s), trip a circuit breaker, and eventually auto-disable the
  webhook. The receiver also **never returns a JSON `message` field** — OMI turns one longer than
  5 chars into a phone push notification. (Bad token → 403, malformed body → 400, oversize → 413;
  the real device never triggers these.)
- Rows land in the **shared** `ambient.db` as `source=omi-<uid>`, `provenance=ambient_overheard`
  (same as the PE — so a future engine scans both). The §3 graduation boundary applies unchanged.

## 2. Edge (conversational) to Genesis — `/v1/voice/*`

The `s2s_bridge` calls Genesis over HTTP for tool execution, the system prompt, and
conversation persistence, against the `/v1/voice/*` routes Genesis exposes.

- **Auth** — `Authorization: Bearer <GENESIS_TOKEN>` (Genesis's HTTP token). Genesis may
  allow token-free access from a trusted network; the bridge always sends the bearer when
  configured.
- **Base URL** — `GENESIS_URL` (the reachable address of the Genesis box from the edge).

This is the only path that crosses from the edge into Genesis today.

## 3. Edge (ambient) to Genesis — graduation API (not yet built)

The ambient bridge is **silent** by design. Nothing it captures reaches Genesis. When the
ambient pipeline matures, a narrow one-way "graduation" boundary will be the *only* place
ambient-derived signal can enter Genesis, carrying explicit provenance (overheard,
low-confidence, possibly misattributed). That boundary is deliberately undefined here
until its design discussion concludes. Until then: ambient data — from the Voice PE (§1) or the
OMI wearable (§1b) — lives and dies in `ambient.db` on the edge.
