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
  cut off playback, `{"type": "disconnect"}` to end the session. (Ambient is one-way; the
  ambient bridge sends no control frames back.)
- **No auth / no handshake.** The socket is expected to live on a trusted local network.
  Do not expose a bridge port to the public internet.
- Keepalive is standard WebSocket ping/pong.

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
until its design discussion concludes. Until then: ambient data lives and dies in
`ambient.db` on the edge.
