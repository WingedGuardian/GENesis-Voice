# meeting_bridge

Real-time diarized capture of a **near-field 1:1 meeting**. A phone (Step 1) or a dedicated
always-on device (Step 2) streams mic audio to this edge bridge, which relays it to Speechmatics
for **streaming transcription + live diarization** and writes a live-updating, diarized `.md`
transcript. This is the capture precondition for Genesis acting on a meeting *mid-call* —
"in two places at once".

Scope is deliberately narrow: **you + the person/computer in front of you**, video call or
face-to-face, a couple of speakers at most. Far-field / noisy-office capture stays the home
Voice PE's job (`ambient_bridge`).

## Why a separate bridge

`ambient_bridge` already has a proven cloud path — `ActiveSession` (Speechmatics streaming +
diarization + enrolled-voice relabel + live `.md`). But its active/passive mode is a **bridge-level
flag** (one device → one connection), so a phone meeting-mic and the always-connected home Voice PE
would fight over it. `meeting_bridge` runs as its own service on its own port and **reuses
`ActiveSession` unchanged** (via a dependency-injected factory), so the two never collide and there
is zero new cloud integration.

## How it works

```
phone capture.html ──wss(16k PCM)──▶ meeting_bridge (aiohttp) ──▶ ActiveSession ──▶ Speechmatics
        ▲  GET /capture/<token>              │  GET /meeting/<token>          │
        └────────── served same-origin ──────┘                     live diarized .md in ~/meeting-sessions
```

- **`GET /capture/<token>`** — serves the phone PWA (`capture.html`). `getUserMedia` → an
  AudioWorklet resamples the mic to 16 kHz Int16 PCM (iOS forces 48 kHz, so it's downsampled
  client-side) → binary frames over a **same-origin** `wss` to `/meeting/<token>`. Screen Wake Lock
  keeps the screen on. **Foreground-only** — a phone browser can't capture in the background; that's
  what the Step-2 dedicated device is for.
- **`GET /meeting/<token>`** — the audio WebSocket. A per-frame energy VAD drives the session
  lifecycle: a cloud `ActiveSession` opens on speech, silent frames are dropped (never billed), and
  the session finalizes after `MEETING_SILENCE_CLOSE_S` of silence — so one connection can span
  several sessions and **each meeting lands in its own transcript** (speaker labels reset across the
  gaps). A `{"type":"marker"}` text frame drops a bookmark (opening a session if none is active).
  `MEETING_VAD_THRESHOLD=0` (default) disables gating → one session for the whole connection.
- **`GET /health`** — unauthenticated liveness only (`{alive, ts}`); **`GET /health/<token>`**
  returns full operational metrics (session/frame counts incl. `frames_gated`) behind the token.
- **Auth** — a constant-time path-token compare (`+ _PREVIOUS` for rotation). The browser can't set
  custom WS headers, so the token rides the URL path. This is the one authenticated public door
  (behind the Tailscale Funnel); `access_log=None` keeps the token out of the logs.

## Config (env)

| Var | Default | Notes |
|---|---|---|
| `MEETING_INGEST_TOKEN` (+`_PREVIOUS`) | — | Path-token auth. Empty ⇒ fail closed. |
| `MEETING_HTTP_HOST` / `MEETING_HTTP_PORT` | `127.0.0.1` / `8790` | Loopback; Funnel is the public door. |
| `MEETING_SM_KEY_PATH` | `~/.ambient-active/speechmatics.key` | Reuses the ambient bridge's key. |
| `MEETING_MODEL` / `MEETING_LANGUAGE` | `enhanced` / `en` | Speechmatics knobs. |
| `MEETING_MAX_SPEAKERS` | auto | Blank ⇒ Speechmatics auto-detects. |
| `MEETING_PREFER_CURRENT_SPEAKER` | `false` | Meeting-tuned: allow re-attributing to a different speaker at a boundary. Set `true` if a room OVER-splits (spurious speaker flips). |
| `MEETING_SPEAKER_SENSITIVITY` | `0.6` | Higher ⇒ splits new speakers more readily. Blank/`none` ⇒ Speechmatics default. Dial down if it over-splits. |
| `MEETING_OUTPUT_DIR` | `~/meeting-sessions` | Live `.md` transcripts land here. |
| `MEETING_SESSION_FACTORY` | `meeting_bridge.session:default_session_factory` | Pluggable cloud backend (swap without a code change). |
| `MEETING_VAD_THRESHOLD` | `0` (off) | Peak int16 energy for "speech". `0` ⇒ gating off (one session per connection). `>0` ⇒ session-per-meeting; calibrate from the peak-log (below). |
| `MEETING_SILENCE_CLOSE_S` | `45` | Finalize a session after this much silence. Keep **below** Speechmatics' idle timeout. |
| `MEETING_VAD_HANGOVER_S` | `0.4` | Keep forwarding this long after speech so word-tails aren't clipped. |
| `MEETING_VAD_LOG_INTERVAL_S` | `30` | Peak/pass/gate summary interval (for calibration); `0` disables. |

**Calibrating the VAD:** deploy with `MEETING_VAD_THRESHOLD=0` (off) and watch the
`meeting vad[..] OFF(observe): … max_peak=.. avg_peak=..` log lines during a real capture — they
show the room's speech vs. silence peaks. Set the threshold between the silence floor and the speech
peaks, then restart to arm it.

## Deploy (edge)

```
deploy/install.sh meeting        # ~/meeting-venv: aiohttp + speechmatics-rt + numpy; stages the unit
mkdir -p ~/.meeting && cp deploy/meeting.env.example ~/.meeting/meeting.env && chmod 600 ~/.meeting/meeting.env
# set MEETING_INGEST_TOKEN, then:
systemctl --user daemon-reload && systemctl --user enable --now meeting-bridge
```

Expose the port through the Tailscale Funnel (public 443/8443/10000 → the loopback port). The phone
loads `https://<edge-host>/capture/<token>`.

Depends only on `aiohttp` + `speechmatics-rt` + `numpy` + stdlib — **no `genesis.*` imports**. The
cloud SDK is imported lazily by the session factory, so the server unit-tests without it.

## Follow-ons (not Step 1)

- Dedicated always-on device (Pi/ESP32) → the "no button to remember" ergonomics.
- Enrolled-name relabel (reuse `SpeakerIDRegistry`; adds ONNX to the venv).
- An `ambient.db` sink alongside the `.md`, so a future attention-engine scan sees meetings too.
- The live consumer that acts on the transcript mid-meeting — the actual payoff.
