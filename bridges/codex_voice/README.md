# Codex voice through SSH

Keep running Codex in your usual Linux SSH terminal. F8 opens the native Windows
microphone and speakers on your PC. A short-lived SSH process connects Codex's
helper control pipe to the matching Windows helper; audio uses the helper's
native WebRTC connection. Manual installation adds no audio recording, listener,
service, scheduled task, or Genesis session registration. The optional managed
mode below adds a background build updater; audio still runs only on demand.

This is an opt-in adapter for **Codex 0.163.0's private helper protocol**. Both
hosts must have the same build commit. Other releases are refused until tested.
It replaces the packaged Linux voice helper with an exec launcher and retains a
hash-verified original. Text sessions continue using the normal Linux client.

## One-time setup

Requirements: Linux Python 3.11+, OpenSSH and npm-installed Codex; Windows Node
and native npm-installed Codex; a default Windows mic/speaker; and an existing
SSH alias that reaches Windows with key authentication. Windows must be awake,
and its SSH command environment must find `node.exe` and `codex.cmd`. The default
Windows OpenSSH command shell is tested. Host keys must already be trusted; the
installer does not weaken host-key checks or set up SSH credentials.
Setup and doctor use Codex's restricted helper environment. `SSH_AUTH_SOCK` is
not inherited: an unencrypted file identity or a working explicit `IdentityAgent`
in your SSH config is needed. Authentication that depends only on the current
terminal's agent socket is rejected during setup. Keep file identities protected
by your existing filesystem permissions; this adapter never creates or copies keys.

From this repository on Linux:

```sh
python3 bridges/codex_voice/bridge.py install --host windows-audio
```

`windows-audio` is your existing private SSH alias. Optional `--identity` selects
an existing key; `--package` selects the physical Linux vendor directory
containing `codex-resources/voice/manifest.json`. `--windows-package` accepts a
Windows vendor directory or the npm `@openai/codex` directory. These flags avoid
guessing when a machine has multiple installations.

The installer configures Windows over SSH automatically. Its immutable host
assets live under `%LOCALAPPDATA%\Genesis\CodexVoice\<content-digest>`. Linux
runtime/configuration lives under `~/.local/share/genesis/codex-voice`; override
it with `--state-dir` on **every** install/doctor/uninstall command if needed.
Keep hosts, keys and local paths out of this public repository.

After setup, keep using **SSH → `codex` → F8**, with no extra terminal windows.
Fleet provisioning can run the same idempotent installer for each Linux host;
multiple hosts can use one Windows PC. Another state directory cannot take over
an already managed Linux helper. Repeated configuration is serialized at the
helper, including callers using different state directories.

## Verify the actual audio path

```sh
python3 bridges/codex_voice/bridge.py doctor
python3 -m pip install 'websockets>=13'
python3 bridges/codex_voice/bridge.py doctor --audio
```

The basic check proves handshake, runtime loading and default-device enumeration
only. `--audio` additionally uses your authenticated local Codex app-server to
create an ephemeral diagnostic thread, negotiate a real WebRTC voice session,
open both Windows devices muted/suppressed, poll them for ten seconds, and stop.
This uses the voice provider and may count toward your account's usage. It
doesn't speak or transmit microphone audio, and doesn't print SDP or transcripts.
The daemon must be running (`codex app-server daemon start`); the default socket
follows `CODEX_HOME` (or `~/.codex` when unset/empty). `--socket` can select
another local app-server Unix socket. A final spoken F8 test verifies perception
and playback. Enumeration alone is deliberately not called an audio success.

## Automatic updates with immediate text startup

Managed mode is opt-in. It keeps the globally installed Codex package untouched
and prepares a private copy of that exact release, paired with an immutable
Windows helper build. It extends the same relay and native protocol checks;
it does not install a separate audio engine or change your Codex configuration.
Windows needs Node, npm, and the existing key-based SSH connection.

```sh
python3 bridges/codex_voice/bridge.py manage \
  --host windows-audio --codex /path/to/npm/codex/bin/codex.js \
  --launcher "$HOME/.local/bin/codex"
python3 bridges/codex_voice/bridge.py status
```

Select the real npm entry point for `--codex`, not the generated launcher. If
that launcher already exists, installation refuses unless
`--replace-launcher-sha256` supplies its exact SHA-256; the original contents
and mode are retained for rollback. An existing manual installation needs a
different `--state-dir` or must be uninstalled first. Use the chosen state
directory on each management command; the generated launcher remembers it.

After an upgrade, `codex` opens immediately for text while a systemd user
oneshot prepares matching builds. Transient network or sleeping-PC failures
retry every five minutes; incompatible manifests, protocol failures and foreign
changes are held for inspection without repeated downloads. `status` reports
the current release's readiness. Relaunch after it reports ready to use voice.
Preparation verifies voice resource hashes, build identity, native handshake,
runtime initialization and device enumeration. It never opens the microphone
or speakers. New helper protocols that fail those checks are not activated.

The existing `doctor --audio` applies to the prepared build's `relay` state
directory shown by `status`. Run the muted device test and a spoken F8 check
when validating a new release: background qualification alone does not prove
a working conversation. Previous immutable builds remain available to active
sessions and are retained for rollback.

```sh
python3 bridges/codex_voice/bridge.py unmanage
```

This stops the updater, restores the previous launcher (or removes the generated
one if none existed), and retains prepared builds. Modified launchers or units
are preserved rather than overwritten. No Genesis session registration or
lifecycle hook is introduced.

Manager source files are fixed within a state directory. Repeating the same
setup is safe; upgrading the manager code itself requires a new state directory
and an explicit launcher replacement hash. This avoids partial source updates
changing a running updater. Native Codex release matching remains automatic.

## Manual updates and uninstall

An npm update can replace the launcher. Update Codex on **both** hosts and rerun
`install`; the installer checks version/build compatibility before patching.
It does not automatically download Codex releases or silently patch untested
versions. Doctor reports when the launcher has been replaced. The installer and
uninstaller refuse foreign helper modifications and retain recovery state.

```sh
python3 bridges/codex_voice/bridge.py uninstall
```

Uninstall restores the verified original with its original permissions. If npm
already replaced it with another manifest-verified original, that newer helper
is preserved. Verified backup files remain beside the packaged helper. Shared
Windows assets are inert after voice stops and are retained: after **all** Linux
clients using a particular digest have been uninstalled, its digest directory
can be removed from `%LOCALAPPDATA%\Genesis\CodexVoice`. Do not remove shared
assets while another client still uses them.

The helper protocol/environment comes from the matching upstream
[Codex voice host](https://github.com/openai/codex/tree/rust-v0.163.0/codex-rs/voice-host)
and [framed protocol](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/realtime-webrtc/src/protocol.rs).
A supported upstream helper-command override could eventually replace the
packaged-file adapter. No Codex binaries or third-party runtime libraries ship here.
