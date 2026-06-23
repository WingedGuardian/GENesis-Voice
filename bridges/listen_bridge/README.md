# Listen Bridge

Explicitly-activated, **silent**, high-accuracy listen-only transcription for the
Voice PE — e.g. for interviews. Distinct from the always-on **ambient** bridge
(local STT, privacy): Listen Mode is opt-in and accuracy-first, so it uses a cloud
STT (Speechmatics realtime, diarized).

```
Voice PE  --(double-press: repoint ambient WS to :8766, wake off, silent)-->
  listen_bridge :8766  --(16k PCM)-->  Speechmatics realtime (diarized)
                       -->  ~/listen-sessions/<ts>.md   (live-updating, speaker-labelled)
```

No Genesis contact, no memory ingestion — the transcript is a **local file** on the
edge you tail/read. (Graduating it into Genesis is deliberately out of scope.)

## Layout
- `config.py` — env-driven `ListenConfig` (port, key path, model, max_speakers, …).
- `server.py` — WS server on `:8766`; each device connection = one Speechmatics realtime
  session; relays PCM via `send_audio`; writes the diarized transcript live.
- `transcript.py` — pure `TranscriptAccumulator` (final/partial message dicts → markdown).
- `feeder.py` — replay a 16k mono wav to validate end-to-end with no device.

## Setup (edge VM)
```bash
python3 -m venv ~/listen-venv
~/listen-venv/bin/pip install speechmatics-rt websockets
mkdir -p ~/.listen-bridge && install -m 600 /dev/stdin ~/.listen-bridge/speechmatics.key  # paste key
cp listen_bridge/systemd/listen-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now listen-bridge
```

## Smoke test (no device)
```bash
ffmpeg -i any.m4a -ac 1 -ar 16000 sample.wav
~/listen-venv/bin/python -m listen_bridge.feeder --wav sample.wav         # real-time pacing
tail -f ~/listen-sessions/*.md                                            # watch it build
```

## Config (env)
| var | default | note |
|---|---|---|
| `LISTEN_WS_PORT` | `8766` | device repoints its ambient WS here |
| `LISTEN_SM_KEY_PATH` | `~/.listen-bridge/speechmatics.key` | Bearer key (chmod 600) |
| `LISTEN_SM_MODEL` | `enhanced` | accuracy model |
| `LISTEN_SM_MAX_DELAY` | `1.0` | latency vs final-context (0.7–2.0) |
| `LISTEN_SM_MAX_SPEAKERS` | `2` | diarization hint |
| `LISTEN_OUTPUT_DIR` | `~/listen-sessions` | transcript files (never commit) |
