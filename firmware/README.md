# Firmware — Voice PE

ESPHome firmware for the Home Assistant Voice PE that gives Genesis its ear on the device.
Derived from the Home Assistant Voice PE ESPHome firmware, with a custom
`voice_assistant_websocket` component that streams audio to a bridge (see
[`../CONTRACTS.md`](../CONTRACTS.md)) and the `"hey genesis"` wake word.

## Two capture paths

1. **Conversational (wake word).** Say "hey genesis" → the component opens a WebSocket to
   the **s2s bridge** (`server_url`) and streams 24 kHz mono PCM for a live OpenAI Realtime
   conversation. This is the original behaviour, unchanged.
2. **Ambient (wake-word-free).** Toggle the **Ambient Mode** switch ON (HA, `entity_category:
   config`, `ALWAYS_OFF` on boot). When ON *and no conversation is active*, the mic audio is
   forked to a SECOND WebSocket — the **ambient bridge** (`ambient_url`, default port 8765) —
   as raw **16 kHz mono** PCM, where it's transcribed locally. A conversation always wins:
   ambient pauses while a wake-word session is RUNNING and resumes when it ends. The send is
   bounded/drop-on-full so a slow ambient bridge can never stall wake-word detection.

   Set `ambient_bridge_url` in `secrets.yaml` (the ambient bridge runs with
   `AMBIENT_INPUT_SR=16000`). Ambient is OFF by default and never survives a reboot — a
   privacy default. Muting the device (hardware switch or software mute) stops capture on
   both paths.

## Contents

- `esphome/components/voice_assistant_websocket/` — the custom streaming component.
- `voice_pe_config.yaml` — the device configuration (substitute your own values).
- `secrets.yaml.example` — template for the install-specific secrets ESPHome injects.

## Build and flash

Built and flashed with the ESPHome toolchain (the ESPHome add-on in Home Assistant, or
the `esphome` CLI). Copy `secrets.yaml.example` to `secrets.yaml`, fill in your values,
then compile and upload `voice_pe_config.yaml`. `secrets.yaml` is gitignored and must
never be committed.

See [`../docs/SETUP.md`](../docs/SETUP.md) for the end-to-end flow.
