"""Pytest setup for ambient_bridge unit tests.

These cover the PURE logic (store schema/verdict math) that runs anywhere. The
sherpa-onnx embedding path is edge-only and validated at E2E, so sherpa_onnx is
stubbed here so ``speaker_id`` imports in the container venv / CI.
"""
import os
import sys
import types

_pkg = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../bridges/ambient_bridge
sys.path.insert(0, _pkg)                       # top-level `import speaker_id` / `import store`
sys.path.insert(0, os.path.dirname(_pkg))      # `.../bridges` → `import ambient_bridge.server`
# Edge-only deps: stub so the modules import in the container/CI. They're only *used* at
# runtime on the bridge (validated at E2E); import-time just needs the names to bind.
for _mod in ("sherpa_onnx", "soxr", "websockets"):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))
# aiohttp (edge-only): the bridge serves a tiny HTTP mode-control endpoint at runtime;
# import-time just needs `from aiohttp import web` to bind.
_aiohttp = types.ModuleType("aiohttp")
_aiohttp.web = types.ModuleType("aiohttp.web")
sys.modules.setdefault("aiohttp", _aiohttp)
sys.modules.setdefault("aiohttp.web", _aiohttp.web)
# speechmatics-rt (edge-only): active-mode cloud session. Stub the names active_session
# binds at module load (they're only USED at runtime, validated at E2E).
_sm = types.ModuleType("speechmatics")
_sm_rt = types.ModuleType("speechmatics.rt")
for _name in ("AsyncClient", "AudioEncoding", "AudioFormat", "ServerMessageType",
              "SpeakerDiarizationConfig", "TranscriptionConfig"):
    setattr(_sm_rt, _name, type(_name, (), {}))
_sm.rt = _sm_rt
sys.modules.setdefault("speechmatics", _sm)
sys.modules.setdefault("speechmatics.rt", _sm_rt)
