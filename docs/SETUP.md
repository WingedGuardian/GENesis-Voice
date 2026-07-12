# Setup

End-to-end setup for the voice edge. The three boxes (see the
[architecture diagram](genesis-voice-architecture.png)) can each be a VM or a container.

## 0. Prerequisites

- A **Home Assistant** install with a **Voice PE** device, plus the **ESPHome** add-on
  (to build and flash firmware).
- A **Voice Edge** box: a dedicated VM or container with Python 3.11+. Not HAOS, not the
  Genesis box. It needs network reach to both the device and the Genesis box.
- A running **Genesis** install (for the conversational path).
- An **OpenAI API key** with Realtime access (for the conversational path).

The edge box should have several GB of RAM. The ambient bridge runs its speech models on
CPU; no GPU is required, though a GPU makes higher-accuracy models viable later.

## 1. Flash the firmware

From `firmware/`: copy `secrets.yaml.example` to `secrets.yaml`, fill in your values, then
compile and upload `voice_pe_config.yaml` with the ESPHome toolchain. This installs the
streaming component and the `"hey genesis"` wake word. `secrets.yaml` is gitignored.

## 2. Stand up the edge bridges

Clone this repo onto the edge box (the systemd units assume `$HOME/genesis-voice`), then:

```bash
deploy/install.sh both       # or: s2s | ambient
```

That creates a venv per bridge, installs dependencies, and stages the systemd user units.

### Conversational (s2s)

```bash
cp bridges/s2s_bridge/edge/.env.example bridges/s2s_bridge/edge/.env
# edit .env: OPENAI_API_KEY, GENESIS_URL, GENESIS_TOKEN, WEBSOCKET_PORT
systemctl --user daemon-reload
systemctl --user enable --now s2s-bridge
```

Run it in the foreground to watch logs the first time:
`bridges/s2s_bridge/edge/run-edge.sh`.

### Ambient (Stage 1, optional)

Download the STT models into `~/models` (see `bridges/ambient_bridge/README.md` for the
exact files: the Silero VAD and the sherpa Zipformer model). Then:

```bash
systemctl --user enable --now ambient-bridge
```

Validate without hardware using the WAV feeder described in the ambient bridge README.
Remember: ambient is capture-only and never contacts Genesis.

## 3. Point the device at the edge

Set the device's bridge address (in `firmware/voice_pe_config.yaml`, your substitutions)
to the edge box and the bridge port (default 8080), then reflash. Say the wake word and
start talking.

## 4. Meeting capture (phone → live diarized transcript)

The `meeting_bridge` captures a near-field 1:1 meeting from your **phone** and streams it to
Speechmatics real-time diarization, writing a live `.md` you can watch grow. It reuses the ambient
bridge's Speechmatics `ActiveSession`, so it needs that key present.

```bash
deploy/install.sh meeting
mkdir -p ~/.meeting && cp deploy/meeting.env.example ~/.meeting/meeting.env
chmod 600 ~/.meeting/meeting.env         # then set a long random MEETING_INGEST_TOKEN
# reuses ~/.ambient-active/speechmatics.key
systemctl --user daemon-reload
systemctl --user enable --now meeting-bridge
```

Expose it to your phone **tailnet-only** (private — not Funnel; the phone is on your tailnet):

```bash
tailscale serve --bg https+insecure://localhost:8790     # serves /capture and /meeting over HTTPS
```

Then either open `https://<edge>.<tailnet>.ts.net/capture/<token>` in the phone browser (works only
while the screen stays on), or — to keep capturing with the **screen locked** — build and sideload
the native Android client:

- Build + install + Samsung background-survival steps: [`../clients/android-mic/README.md`](../clients/android-mic/README.md).
- Deliver the built `app-debug.apk` to the phone over the tailnet (e.g. `tailscale serve` a
  download path, or `python -m http.server` on the edge behind serve), then sideload it.

The transcript lands in `~/meeting-sessions/<timestamp>.md` on the edge. This path is the user's own
capture — it goes straight to a file and never touches `ambient.db` or the §3 graduation boundary.

## Notes

- Keep bridge ports on a trusted local network. The device-to-edge socket is unauthenticated
  by design (see [`../CONTRACTS.md`](../CONTRACTS.md)). The meeting bridge (§1c) is the exception —
  it is path-token authenticated because it rides the tailnet to your phone.
- `bridges/s2s_bridge` can also run as a container — see its `Dockerfile`
  (`docker build` then `docker run --env-file edge/.env -p 8080:8080`). The systemd
  path above is the simplest for a VM.
