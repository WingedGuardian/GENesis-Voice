#!/usr/bin/env bash
# Fail if private data leaks into this public repo: LAN/CGNAT IPs, the Tailscale ULA
# prefix, personal-provider emails, or common API-key prefixes. Generic patterns only
# (no specific values are stored here). Run in CI and locally before pushing.
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
echo "No private data found."
