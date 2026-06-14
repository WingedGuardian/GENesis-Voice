"""Ambient sensory service — standalone capture→transcribe→(diarize)→store bridge.

Runs on the dedicated bridge VM (`assistant1`), NOT in the Genesis container, and
talks to Genesis only via a future graduation boundary (not built). Depends only on
sherpa-onnx / websockets / soxr / numpy / soundfile / stdlib — NO `genesis.*` imports.
"""
