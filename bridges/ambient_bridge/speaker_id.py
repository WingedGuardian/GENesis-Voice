"""Speaker-ID registry for ambient capture — match utterances to enrolled voiceprints.

Standalone (sherpa-onnx + numpy + stdlib only; no genesis imports). Holds ONE
``SpeakerEmbeddingExtractor`` (the same eres2net 16k model the diarizer uses) and a
``{name: voiceprint}`` dict persisted as JSON. The diar worker calls ``embed`` per
utterance, then ``score`` on the individual embedding (direct verdict, for utts
>= min_embed_s) and on a diar cluster's mean embedding (aggregation verdict, to
recover short utts), thresholding at ``user_verify_threshold``.

Why a separate extractor (not the diarizer's): ``OfflineSpeakerDiarization`` does not
expose per-segment embeddings, so we run our own — same ONNX, ~40 MB, negligible RAM.

Calibration (Stage-0 16k gate, 2026-06-20): eres2net, threshold ~0.35 (precision-first),
direct verdicts reliable for utterances >= ~3 s; shorter utts are recovered by the
cluster-centroid mean (averaging N noisy short embeddings reduces variance ~sqrt(N)).
"""
from __future__ import annotations

import json
import logging
import os
import threading

import numpy as np
import sherpa_onnx

logger = logging.getLogger("ambient.speaker_id")

TARGET_SR = 16000


class SpeakerIDRegistry:
    """Enrolled voiceprints + a shared embedding extractor. Thread-safe embedding
    (the ONNX session is serialized); voiceprints are mutated only at enroll time."""

    def __init__(
        self,
        model_path: str,
        *,
        persist_path: str,
        num_threads: int = 1,
        user_name: str = "user",
        sample_rate: int = TARGET_SR,
    ) -> None:
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=model_path, num_threads=num_threads)
        if not cfg.validate():
            raise RuntimeError(f"invalid SpeakerEmbeddingExtractorConfig (model={model_path})")
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self._dim = int(self._extractor.dim)
        self._sr = sample_rate
        self._user_name = user_name
        self._persist_path = os.path.expanduser(persist_path)
        self._voiceprints: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()
        self._load()
        logger.info(
            "SpeakerIDRegistry loaded (model=%s dim=%d speakers=%s)",
            os.path.basename(model_path), self._dim, list(self._voiceprints),
        )

    # --- properties -----------------------------------------------------------

    @property
    def dim(self) -> int:
        return self._dim

    def names(self) -> list[str]:
        return list(self._voiceprints)

    def has_user(self) -> bool:
        return self._user_name in self._voiceprints

    # --- embedding ------------------------------------------------------------

    def embed(self, samples: np.ndarray) -> np.ndarray | None:
        """One L2-normalized embedding for an utterance, or None if too short
        (extractor not ready). Thread-safe: the shared ONNX session is serialized."""
        if samples is None or len(samples) == 0:
            return None
        with self._lock:
            stream = self._extractor.create_stream()
            stream.accept_waveform(self._sr, samples)
            stream.input_finished()
            if not self._extractor.is_ready(stream):
                return None
            vec = np.asarray(self._extractor.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm else vec

    @staticmethod
    def mean_embedding(embeddings: list[np.ndarray]) -> np.ndarray | None:
        """L2-normalized mean of per-utterance embeddings (a cluster centroid).
        Averaging N noisy short-utterance embeddings reduces variance ~sqrt(N),
        which is what makes short-utterance recovery reliable."""
        embs = [e for e in embeddings if e is not None]
        if not embs:
            return None
        m = np.mean(embs, axis=0)
        norm = float(np.linalg.norm(m))
        return (m / norm if norm else m).astype(np.float32)

    # --- scoring --------------------------------------------------------------

    def score(self, emb: np.ndarray | None, name: str | None = None) -> float:
        """Cosine similarity of ``emb`` to a stored voiceprint (both L2-normalized →
        dot product). Returns -1.0 if the embedding or voiceprint is missing."""
        if emb is None:
            return -1.0
        vp = self._voiceprints.get(name or self._user_name)
        if vp is None:
            return -1.0
        return float(np.dot(emb, vp))

    def verify(self, emb: np.ndarray | None, threshold: float, name: str | None = None) -> bool:
        """True if ``emb`` matches the voiceprint at/above ``threshold``."""
        return bool(self.score(emb, name) >= threshold)

    def classify_window(
        self,
        embeddings: list[np.ndarray | None],
        durations: list[float],
        clusters: list[int],
        *,
        threshold: float,
        min_embed_s: float,
    ) -> list[tuple[bool | None, str | None]]:
        """Per-utterance (is_user, method) for one diar window. Pure decision logic
        (no ONNX) so it is unit-testable.

        - DIRECT verdict for utts with a usable embedding AND duration >= min_embed_s
          (the individual embedding is reliable) → method 'direct'.
        - The remaining (short) utts inherit their diar CLUSTER's centroid verdict
          → method 'cluster'. The centroid is taken over ALL the cluster's usable
          embeddings (incl. any long/clean ones), which anchors the short utts.
        - No usable embedding → (None, None) (leave is_user NULL).

        ``embeddings``/``durations``/``clusters`` are aligned, one entry per utterance.
        """
        n = len(embeddings)
        verdicts: list[tuple[bool | None, str | None]] = [(None, None)] * n
        for i in range(n):
            if embeddings[i] is not None and durations[i] >= min_embed_s:
                verdicts[i] = (bool(self.score(embeddings[i]) >= threshold), "direct")
        # Cluster centroid for the still-undecided utts (anchored by the whole cluster).
        groups: dict[int, list[int]] = {}
        for i in range(n):
            if embeddings[i] is not None:
                groups.setdefault(clusters[i], []).append(i)
        for idxs in groups.values():
            undecided = [i for i in idxs if verdicts[i][1] is None]
            if not undecided:
                continue
            centroid = self.mean_embedding([embeddings[i] for i in idxs])
            is_user = bool(self.score(centroid) >= threshold)
            for i in undecided:
                verdicts[i] = (is_user, "cluster")
        return verdicts

    # --- enrollment + persistence --------------------------------------------

    def enroll(self, name: str, samples_list: list[np.ndarray]) -> int:
        """Enroll/replace ``name`` from a list of utterance waveforms: embed each,
        take the L2-normalized centroid, persist. Returns the count of usable clips."""
        embs = [e for s in samples_list if (e := self.embed(s)) is not None]
        if not embs:
            raise ValueError(f"no usable enrollment audio for {name!r}")
        centroid = self.mean_embedding(embs)
        self._voiceprints[name] = centroid
        self._save()
        logger.info("enrolled %r from %d/%d clips", name, len(embs), len(samples_list))
        return len(embs)

    def _save(self) -> None:
        data = {
            "dim": self._dim,
            "speakers": [
                {"name": k, "embedding": v.astype(float).tolist()}
                for k, v in self._voiceprints.items()
            ],
        }
        tmp = self._persist_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self._persist_path)

    def _load(self) -> None:
        if not os.path.exists(self._persist_path):
            return
        try:
            with open(self._persist_path) as f:
                data = json.load(f)
        except (ValueError, OSError):
            logger.warning("could not read speaker registry %s — starting empty",
                           self._persist_path, exc_info=True)
            return
        saved_dim = data.get("dim")
        if saved_dim is not None and int(saved_dim) != self._dim:
            logger.warning(
                "registry dim %s != model dim %d — ignoring stale voiceprints",
                saved_dim, self._dim,
            )
            return
        for sp in data.get("speakers", []):
            try:
                self._voiceprints[sp["name"]] = np.asarray(sp["embedding"], dtype=np.float32)
            except (KeyError, TypeError, ValueError):
                logger.warning("skipping malformed registry entry: %r", sp)
