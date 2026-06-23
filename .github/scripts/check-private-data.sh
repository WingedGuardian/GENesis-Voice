#!/usr/bin/env bash
# Fail if private data leaks into this public repo:
#   1. TEXT patterns — LAN/CGNAT IPs, the Tailscale ULA prefix, personal-provider
#      emails, or common API-key prefixes (generic patterns only; no real values here).
#   2. BINARY/DATA artifacts — biometric voiceprints, audio, and DB files. These match
#      none of the text patterns, so a `git add -f` past .gitignore would slip a
#      voiceprint or recording into this PUBLIC repo unnoticed. Block them outright.
# Run in CI and locally (pre-commit) before pushing.
set -uo pipefail

PATTERN='192\.168\.[0-9]{1,3}\.[0-9]{1,3}'
PATTERN+='|(^|[^0-9.])10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}'
PATTERN+='|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}'
PATTERN+='|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}'
PATTERN+='|fd7a:115c:a1e0'
PATTERN+='|[a-z0-9._%+-]+@(gmail|yahoo|outlook|hotmail|proton|icloud)\.com'
PATTERN+='|sk-[A-Za-z0-9]{16}|hf_[A-Za-z0-9]{16}|ghp_[A-Za-z0-9]{16}|AKIA[0-9A-Z]{16}'

# Documented placeholders / loopback / generic network addresses are allowed.
ALLOW='example|placeholder|Replace|your-|<user>|<edge-host>|127\.0\.0\.1|0\.0\.0\.0|10\.0\.0\.0'

hits=$(grep -rnIE "$PATTERN" . \
  --exclude-dir=.git --exclude-dir=.github 2>/dev/null \
  | grep -vIE "$ALLOW" || true)

if [ -n "$hits" ]; then
  echo "::error::Private data detected — scrub before committing to a public repo:"
  echo "$hits"
  exit 1
fi

# --- Binary/data-artifact guard ----------------------------------------------
# Voiceprints, audio, and DB files match NONE of the text patterns above, so the
# scan would pass them. Block any TRACKED file of these types (catches `git add -f`
# bypasses of .gitignore). Uses the git index, so untracked local data is ignored.
data_globs=(
  '*.wav' '*.flac' '*.pcm' '*.mp3' '*.m4a' '*.ogg' '*.opus'                 # audio
  '*.db' '*.db-wal' '*.db-shm' '*.sqlite' '*.sqlite3'                       # databases
  '*.onnx'                                                                  # models
  '*speaker_registry*.json' 'ambient_enroll_*.json' 'ambient_health.json'   # voiceprint/runtime
)
# Allowlist: documented exceptions as git pathspecs. EMPTY by default — add a
# ':(exclude)path' entry to permit ONE specific legit file (e.g. a tiny test
# fixture) without weakening the guard for everything else. Example:
#   data_allow=( ':(exclude)tests/fixtures/sample.wav' )
data_allow=()
data_hits=$(git ls-files -z -- "${data_globs[@]}" "${data_allow[@]}" 2>/dev/null | tr '\0' '\n')
if [ -n "$data_hits" ]; then
  echo "::error::Binary/data artifacts must never be committed to this public repo"
  echo "::error::(voiceprints, audio, databases). Remove and add to .gitignore:"
  echo "$data_hits"
  exit 1
fi

echo "No private data found."
