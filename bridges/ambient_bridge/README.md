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

**Speaker identification** runs in the same deferred diar worker: each utterance is matched
against ALL enrolled voiceprints (best cosine match) and tagged with `speaker_name` (the
matched name, or NULL if none ≥ threshold) plus `is_user` (1 = the matched speaker is the
configured user, else 0; NULL = no verdict). A DIRECT match is used for utterances ≥
`AMBIENT_MIN_EMBED_S` (3 s, where embeddings are reliable); shorter utterances inherit their
diar cluster's centroid verdict (averaging the cluster's embeddings recovers short-utterance
recall). The method (`direct`|`cluster`) is recorded in the row's `meta`. `is_user` is the
user-only graduation gate; `speaker_name` is additive (family/guests). Disabled or no voiceprint
enrolled → both stay NULL (capture unaffected). Calibration (threshold 0.35, min 3 s) is from the
16 kHz speaker-ID gate, validated human-vs-human (a 2nd speaker never crossed 0.35); env-tunable.

Enroll a speaker (on the edge box, from `~/genesis-voice/bridges`):
```bash
# (a) ONLINE — no teardown: the running bridge captures + enrolls; ambient keeps capturing.
#     The default + recommended path for adding family/guests.
~/ambient-venv/bin/python -m ambient_bridge.enroll --name alice --online   # then speak ~30s
# (b) from existing 16k wavs (no re-recording):
~/ambient-venv/bin/python -m ambient_bridge.enroll --name user --from-dir ~/enroll_clips
# (c) offline live capture (stop the bridge first so :8765 is free, then speak):
~/ambient-venv/bin/python -m ambient_bridge.enroll --name bob
```
Online enroll writes a request file the bridge polls (`~/ambient_enroll_request.json`) and waits
for its result (`~/ambient_enroll_result.json`); the bridge backs up the registry to `*.bak`
before each overwrite (rollback). All voiceprint/enrollment artifacts live in `~/` (never the repo).

Wire contract mirrors the firmware exactly: binary frames = 16-bit mono PCM; JSON text
frames (`{"type":"interrupt"|"disconnect"}`) for control. No auth/handshake. See
[`../../CONTRACTS.md`](../../CONTRACTS.md).

## Deploy to the edge box
Deploy is manual (there is no CI/Supervisor for this). Stale code deploys silently, so
always re-run the feeder smoke test after deploying.

```bash
# one-time: venv + deps + models (deploy/install.sh ambient does the venv + deps)
python3 -m venv ~/ambient-venv
~/ambient-venv/bin/pip install sherpa-onnx onnxruntime soxr soundfile websockets numpy aiohttp speechmatics-rt
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

## Active mode (cloud — "Listen Mode")
A dual-mode listening service. **Passive** (default) = the local Zipformer path above (private,
fast). **Active** = high-accuracy CLOUD transcription (Speechmatics realtime, diarized) for e.g.
interviews.

- The device POSTs the mode to the HTTP control endpoint on its Active-Listening toggle:
  `POST http://<edge>:8767/mode {"mode":"active"|"passive"}`. One device → one connection → a
  bridge-level flag. **Default passive**, so a dropped/late POST fails safe to LOCAL.
- Active opens a Speechmatics session that writes a live, diarized transcript to
  `~/listen-sessions/<ts>.md` (DISTINCT from `ambient.db`). Every cloud-session open/close is
  **loud-logged** — ambient audio never reaches the cloud without an explicit active POST.
- Needs a Speechmatics key at `~/.ambient-active/speechmatics.key` (chmod 600). Active env:
  `AMBIENT_CONTROL_HTTP_PORT` (8767) · `AMBIENT_ACTIVE_SM_KEY_PATH` · `AMBIENT_ACTIVE_OUTPUT_DIR`
  (`~/listen-sessions`) · `AMBIENT_ACTIVE_MODEL` (enhanced) · `AMBIENT_ACTIVE_MAX_SPEAKERS` (2).

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

Speaker-ID: `AMBIENT_SPEAKER_ID_ENABLED` (1) · `AMBIENT_USER_VERIFY_THRESHOLD` (0.35) ·
`AMBIENT_MIN_EMBED_S` (3.0) · `AMBIENT_SPEAKER_REGISTRY` (~/ambient_speaker_registry.json) ·
`AMBIENT_USER_SPEAKER_NAME` (user) · `AMBIENT_SPEAKER_ID_MODEL` (auto-detect *eres2net*16k* if unset).

Online enroll: `AMBIENT_ENROLL_REQUEST` / `AMBIENT_ENROLL_RESULT` (~/ambient_enroll_*.json) ·
`AMBIENT_ENROLL_CHECK_S` (2.0) · `AMBIENT_ENROLL_MIN_DUR_S` (1.0) · `AMBIENT_ENROLL_TARGET_S` (30) ·
`AMBIENT_ENROLL_MAX_WAIT_S` (120).

## Not yet (tracked in the design)
The filter / attention / sense-making tiers and the graduation boundary to Genesis memory.
Cross-time speaker identity now EXISTS (the `is_user` voiceprint registry above); cross-window
diar *cluster* identity (comparable cluster ids across windows) is still not built — `is_user`
sidesteps it by matching a fixed enrolled voiceprint.
