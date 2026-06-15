# Ambient Bridge — sensory capture service (Stage 1)

Always-on **ambient listening** capture for the Voice PE. A bounded sensory service that
runs on the **Voice Edge box** (a dedicated VM or container), NOT in the Genesis box, and
is **silent** in Stage 1: it captures, transcribes, and stores to its own isolated
`ambient.db`. No Genesis-memory contact. There is **no shared import** with Genesis.

## Pipeline
`WS (raw 16-bit PCM, 24 kHz) -> soxr 24->16k -> sherpa Silero VAD -> Zipformer STT -> ambient.db`
(+ rolling TTL/row-ceiling purge, + a health heartbeat file).

**Speaker diarization (Stage-1b)** runs DEFERRED, off the ingest path: utterances are
batched into continuous windows; each closed window is diarized by a bounded async worker
(sherpa pyannote-segmentation + 3dspeaker embedding + clustering), and each utterance gets
a `speaker_label` of the form `wN:c/total` — window N, cluster c of `total` speakers found.
Labels are window-scoped, NOT comparable across windows or connections (no cross-time
identity in Stage-1). Validated on English (the zh-cn eres2net embedding is
language-agnostic). If the diar models are absent it degrades to capture-only (label NULL).

Wire contract mirrors the firmware exactly: binary frames = 16-bit mono PCM; JSON text
frames (`{"type":"interrupt"|"disconnect"}`) for control. No auth/handshake. See
[`../../CONTRACTS.md`](../../CONTRACTS.md).

## Deploy to the edge box
Deploy is manual (there is no CI/Supervisor for this). Stale code deploys silently, so
always re-run the feeder smoke test after deploying.

```bash
# one-time: venv + deps + models (deploy/install.sh ambient does the venv + deps)
python3 -m venv ~/ambient-venv
~/ambient-venv/bin/pip install sherpa-onnx onnxruntime soxr soundfile websockets numpy
wget -P ~/models https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx
# Zipformer STT model dir expected at ~/models/sherpa-zip (encoder/decoder/joiner/tokens)
# Diarization (Stage-1b) models in ~/models (deploy/install.sh ambient downloads these):
#   sherpa-onnx-pyannote-segmentation-3-0/model.onnx   (segmentation)
#   3dspeaker_speech_eres2net_*_16k.onnx               (speaker embedding, English-validated)

# copy the package onto the edge box (adjust user@host + paths for your setup)
rsync -a --delete bridges/ambient_bridge/ <user>@<edge-host>:~/genesis-voice/bridges/ambient_bridge/

# run (foreground)
cd ~/genesis-voice/bridges && ~/ambient-venv/bin/python -m ambient_bridge.server

# OR as a user service (see deploy/systemd/ambient-bridge.service)
cp deploy/systemd/ambient-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now ambient-bridge
```

## Smoke test (canonical "did my code reach the box?")
```bash
# with the server running:
~/ambient-venv/bin/python -m ambient_bridge.feeder --wav ~/sample60.wav
sqlite3 ~/ambient.db "SELECT ts, duration_s, text FROM ambient_transcripts ORDER BY id DESC LIMIT 5;"
cat ~/ambient_health.json   # alive, utterances_total, db rows
```

## Config (env, see `config.py`)
`AMBIENT_WS_PORT` (8765) · `AMBIENT_INPUT_SR` (24000; set 16000 if the ambient firmware
sends raw 16k) · `AMBIENT_VAD_MIN_SILENCE` (0.4) · `AMBIENT_TTL_HOURS` (48) ·
`AMBIENT_ROW_CEILING` (200000) · `AMBIENT_*` model paths.

Diarization: `AMBIENT_DIAR_ENABLED` (1) · `AMBIENT_DIAR_THRESHOLD` (0.7; higher = fewer
clusters) · `AMBIENT_DIAR_WINDOW_S` (60) · `AMBIENT_DIAR_QUEUE_MAX` (4) ·
`AMBIENT_DIAR_NUM_THREADS` (2) · `AMBIENT_SEG_MODEL` / `AMBIENT_EMB_MODEL` (auto-detected
in the models dir if unset).

## Not yet (tracked in the design)
The filter / attention / sense-making tiers, the graduation boundary to Genesis memory, a
Genesis-side health probe of `ambient_health.json`, cross-window / cross-time speaker
identity (a speaker-embedding registry), and the ESP32 ambient-mode firmware (the real
audio source; Stage 1 uses the test feeder).
