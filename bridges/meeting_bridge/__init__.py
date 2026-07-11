"""Meeting bridge — real-time diarized capture of a near-field 1:1 meeting.

Runs on the Voice Edge box (NOT the Genesis container, NOT HAOS), on its OWN port and venv,
deliberately separate from the 24/7 ambient service so a phone meeting-mic and the always-on
home Voice PE never collide over the ambient bridge's single active/passive mode flag.

A phone (or, later, a dedicated device) streams raw 16 kHz PCM over an authenticated WebSocket;
the bridge relays it to Speechmatics for real-time streaming transcription + live diarization,
reusing the ambient bridge's proven ``ActiveSession`` (which writes a live-updating, diarized
``.md`` transcript). This is the capture precondition for Genesis acting on a meeting mid-call.

Depends only on aiohttp + speechmatics-rt + numpy + stdlib — NO ``genesis.*`` imports. The cloud
session is dependency-injected (``session.py``) so the server unit-tests without the cloud SDK.
"""
