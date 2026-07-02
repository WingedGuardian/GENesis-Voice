"""Opt-out of onnxruntime's CPU (BFC) memory arena for the variable-shape model sessions.

Why: the ORT arena grows monotonically under variable-length audio inputs and never returns
memory to the OS (bfc_arena.cc frees a region only when EVERY chunk in it is unused — rare in
practice), producing the residual activity-driven RSS ratchet that survived MALLOC_ARENA_MAX=2
(wrong layer: glibc caps can't see arena-held memory, and neither can malloc_trim). Measured
on-edge (E3 bench, real models + the real utterance-length distribution, 2026-07-02): over 400
utterances the embedder ratchets 158→545 MB with the arena ON vs flat ~162 MB with it OFF, and
the offline Zipformer 166→444 vs flat ~212 — at ~4.5% RTF cost (0.076→0.079 embed,
0.089→0.093 STT; both ≪ real-time).

Mechanism: sherpa-onnx parses ``provider="cpu:<conf-file>"`` (session.cc
``SplitProviderAndConfig``, present at the pinned v1.13.2) and applies ``EnableCpuMemArena=0``
/ ``EnableMemPattern=0`` to the session options; a session.h template routes EVERY model
config class through that path. VAD is deliberately NOT routed through this: its inputs are
fixed-shape (512-sample chunks — no arena growth to fix) and it sits on the hot capture path.

Both the parent and the diar spawn child call ``ort_provider`` (the child re-derives config
from the inherited env), so the conf write must be race-safe: atomic tmp+rename with a
per-pid tmp name. FAIL OPEN: if the conf can't be materialised, log and return plain "cpu"
(the arena stays on) — capture must never die for a memory optimisation.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("ambient.ort_session")

_CONF_BODY = "EnableCpuMemArena=0\nEnableMemPattern=0\n"


def ort_provider(cfg) -> str:
    """The sherpa ``provider`` string for the variable-shape model sessions."""
    if not cfg.ort_arena_off:
        return "cpu"
    path = cfg.ort_conf_path
    try:
        try:
            with open(path) as f:
                current = f.read()
        except OSError:
            current = None
        if current != _CONF_BODY:
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                f.write(_CONF_BODY)
            os.replace(tmp, path)
        return f"cpu:{path}"
    except OSError:
        logger.warning("ORT arena-off requested but conf unwritable at %s — arena stays ON",
                       path, exc_info=True)
        return "cpu"
