"""OMI wearable ingest bridge — a mic-only ambient capture peer of the Voice PE.

Runs on the Voice Edge box (NOT the Genesis container, NOT HAOS). Receives OMI's
real-time transcript webhook and writes normalized utterances into the SAME isolated
``ambient.db`` the ambient bridge uses (``source=omi-<uid>``). Stage 1 — capture only;
it never contacts Genesis. Depends only on aiohttp + stdlib — NO ``genesis.*`` imports.

Schema is shared with the ambient bridge by importing its ``AmbientStore`` (stdlib-only,
single source of truth for the ``ambient_transcripts`` table) — see ``server.py``.
"""
