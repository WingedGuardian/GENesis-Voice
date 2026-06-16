"""Pytest path setup so ``from app import ...`` resolves from the bridge root.

The service is launched as ``python -m app.main`` with this directory as the
working directory, so tests import the same way. No package install needed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
