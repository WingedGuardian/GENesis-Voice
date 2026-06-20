"""Pytest setup for ambient_bridge unit tests.

These cover the PURE logic (store schema/verdict math) that runs anywhere. The
sherpa-onnx embedding path is edge-only and validated at E2E, so sherpa_onnx is
stubbed here so ``speaker_id`` imports in the container venv / CI.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.modules.setdefault("sherpa_onnx", types.ModuleType("sherpa_onnx"))
