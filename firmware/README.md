# Firmware — Voice PE

ESPHome firmware for the Home Assistant Voice PE that gives Genesis its ear on the device.
Derived from the Home Assistant Voice PE ESPHome firmware, with a custom
`voice_assistant_websocket` component that streams audio to a bridge (see
[`../CONTRACTS.md`](../CONTRACTS.md)) and the `"hey genesis"` wake word.

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
