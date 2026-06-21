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
