"""Pure speaker-identity resolution for ACTIVE (cloud) mode — numpy/stdlib only, no SDK/model.

Maps Speechmatics positional speaker labels (S1/S2/…) to enrolled identities by embedding each
speaker's RECENT audio (via injected callbacks) and matching it against the voiceprint registry.

Design (de-risked 2026-06-24): NOT permanent-sticky. Each qualifying speaker is (re)checked on a
cadence (``recheck_s``); the assignment is REVERTED to positional once its recent audio stops
matching an enrolled voiceprint. So if Speechmatics REUSES a label for a different person
mid-session, the wrong name shows for AT MOST ``recheck_s`` seconds before reverting — never a
PERMANENT wrong name; the steady state is always correct-or-positional. V1 enrolls only the user,
so the only name ever assigned is the user's — everyone else stays positional.

The two injected callables hide ALL I/O + ONNX (slice-the-ring+embed, and registry.best_match),
so this module unit-tests with plain lambdas — mirroring words_to_runs / _diar_kwargs / classify_window.
"""
from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable

import numpy as np


class ActivePcmRing:
    """Bounded ring of recent 16k float32 PCM, keyed by cumulative-sample elapsed seconds.

    Memory is capped at ~``capacity_s`` regardless of session length. ``append`` is O(1) and runs
    on the event loop (per relay frame); ``slice`` runs in a worker thread (only on resolve). The
    two cross threads, so a lock guards the deque — but it's held only for the deque mutation /
    a cheap reference snapshot (the sample arrays are immutable once appended), so the event loop
    is never blocked on the embedding work. Elapsed is the CUMULATIVE appended-sample count ÷ sr,
    which IS Speechmatics' audio clock (drift-free vs. wall time under relay jitter)."""

    def __init__(self, capacity_s: float, sample_rate: int = 16000) -> None:
        self._sr = sample_rate
        self._cap = int(capacity_s * sample_rate)
        self._chunks: deque[tuple[int, np.ndarray]] = deque()  # (start_sample_index, samples)
        self._total = 0      # cumulative samples ever appended (the session audio clock)
        self._buffered = 0   # samples currently retained
        self._lock = threading.Lock()

    def append(self, frame: bytes) -> None:
        s = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        if s.size == 0:
            return
        with self._lock:
            self._chunks.append((self._total, s))
            self._total += s.size
            self._buffered += s.size
            while self._buffered > self._cap and len(self._chunks) > 1:
                _, old = self._chunks.popleft()
                self._buffered -= old.size

    @property
    def elapsed_s(self) -> float:
        with self._lock:
            return self._total / self._sr

    def slice(self, start_s: float, end_s: float) -> "np.ndarray | None":
        """Concatenated float32 samples for [start_s, end_s), or None if nothing retained overlaps.
        Snapshots the chunk refs under the lock (cheap), then walks them lock-free (arrays are
        immutable once appended), so the loop's append is never blocked on the concatenate."""
        a, b = int(start_s * self._sr), int(end_s * self._sr)
        if b <= a:
            return None
        with self._lock:
            chunks = list(self._chunks)
        parts: list[np.ndarray] = []
        for start_idx, samp in chunks:
            lo, hi = max(a, start_idx), min(b, start_idx + samp.size)
            if hi > lo:
                parts.append(samp[lo - start_idx: hi - start_idx])
        return np.concatenate(parts) if parts else None


def recent_spans(
    speaker: str,
    spans: list[tuple[str, float, float]],
    now_elapsed: float,
    window_s: float,
) -> list[tuple[float, float]]:
    """A speaker's audio spans clipped to the last ``window_s`` seconds → [(start, end), …]
    (oldest→newest), empty if the speaker has no audio in the window. Caps the audio we embed
    per check so cost is bounded regardless of how long the speaker has been talking."""
    lo = max(0.0, now_elapsed - window_s)
    out: list[tuple[float, float]] = []
    for spk, s, e in spans:
        if spk != speaker:
            continue
        cs, ce = max(s, lo), min(e, now_elapsed)
        if ce > cs:
            out.append((cs, ce))
    return out


class SpeakerResolver:
    """Builds + maintains the S#→identity map for one active session (continuous re-verify)."""

    def __init__(self, *, min_speaker_s: float, recheck_s: float, embed_window_s: float) -> None:
        self._min_speaker_s = min_speaker_s
        self._recheck_s = recheck_s
        self._embed_window_s = embed_window_s
        self.assigned: dict[str, str] = {}        # S-label -> enrolled name (live; read at render)
        self._last_check: dict[str, float] = {}   # S-label -> session-elapsed of last embed attempt

    def resolve(
        self,
        spans: list[tuple[str, float, float]],
        *,
        now_elapsed: float,
        embed_spans_fn: Callable[[list[tuple[float, float]]], "np.ndarray | None"],
        match_fn: Callable[["np.ndarray"], tuple[str | None, float]],
    ) -> set[str]:
        """(Re)check each speaker with enough recent audio; update ``assigned`` in place. Returns
        the set of S-labels whose assignment CHANGED this call (for logging). Pure bookkeeping:
        ``embed_spans_fn`` does ring-slice+embed+mean, ``match_fn`` does best_match — both injected.
        A speaker checked within ``recheck_s`` is skipped (don't re-embed); one whose recent audio
        no longer matches an enrolled voiceprint is REVERTED to positional."""
        changed: set[str] = set()
        for speaker in {spk for spk, _, _ in spans}:
            last = self._last_check.get(speaker)
            if last is not None and (now_elapsed - last) < self._recheck_s:
                continue  # checked recently — leave its current label as-is
            spk_spans = recent_spans(speaker, spans, now_elapsed, self._embed_window_s)
            if sum(e - s for s, e in spk_spans) < self._min_speaker_s:
                continue  # not enough RECENT audio to identify/re-verify confidently
            emb = embed_spans_fn(spk_spans)
            self._last_check[speaker] = now_elapsed  # count the attempt either way (avoid hammering)
            if emb is None:
                continue  # audio too short to embed (unexpected at >=min_speaker_s) / evicted
            name, _score = match_fn(emb)
            prev = self.assigned.get(speaker)
            if name:
                if prev != name:
                    self.assigned[speaker] = name
                    changed.add(speaker)
            elif prev is not None:
                del self.assigned[speaker]  # no longer matches → REVERT to positional
                changed.add(speaker)
        return changed
