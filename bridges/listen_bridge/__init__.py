"""Listen Mode bridge — explicitly-activated, silent, high-accuracy capture.

The Voice PE (on a double-press) repoints its ambient WS stream to this bridge's
port; the bridge relays the 16k PCM to the Speechmatics realtime API (diarized)
and writes a live-updating, speaker-labelled transcript file on the edge. No
Genesis contact, no memory ingestion — the transcript is a local artifact.

Standalone: runs in its own venv on the edge VM. Deps: speechmatics-rt,
websockets, stdlib. Do NOT import genesis.* here.
"""
