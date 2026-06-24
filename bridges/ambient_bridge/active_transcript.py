"""Diarized transcript accumulator for ACTIVE (cloud) mode — pure + testable.

Consumes Speechmatics realtime ``AddTranscript`` (final) and ``AddPartialTranscript``
(provisional) message dicts and renders a live-updating, speaker-labelled markdown
transcript. No I/O, no SDK types — so it unit-tests with hand-built message dicts.
(Re-homed from the standalone listen_bridge — Option C merges active mode here.)
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Sentinel "speaker" for a user-dropped transcript marker (a single-press bookmark).
# Distinct from any Speechmatics label (S1/S2/?) or the SYSTEM error line, so it never
# collides with — or merges into — a real speaker run.
_MARKER = "__MARKER__"


def _fmt_ts(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def words_to_runs(results: list | None) -> list[tuple[str, float, str]]:
    """Group a Speechmatics ``results`` list (words/punctuation) into consecutive
    same-speaker runs → ``[(speaker, start_time, text), ...]``. Punctuation attaches
    to the preceding word with no leading space; leading punctuation is dropped."""
    runs: list[tuple[str, float, str]] = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        alts = r.get("alternatives") or []
        if not alts:
            continue
        content = (alts[0].get("content") or "").strip()
        if not content:
            continue
        if r.get("type") == "punctuation":
            if runs:
                spk, start, text = runs[-1]
                runs[-1] = (spk, start, text + content)
            continue
        speaker = alts[0].get("speaker") or "?"
        if runs and runs[-1][0] == speaker:
            spk, start, text = runs[-1]
            runs[-1] = (spk, start, text + " " + content)
        else:
            runs.append((speaker, float(r.get("start_time", 0.0)), content))
    return runs


def runs_with_spans(
    runs: list[tuple[str, float, str]], now_elapsed: float
) -> list[tuple[str, float, float]]:
    """Add an end_time to each run for audio-span extraction → ``[(speaker, start, end), …]``.
    A run lasts until the NEXT run starts (Speechmatics gives no per-word end_time); the last
    run extends to ``now_elapsed``. Marker sentinels are skipped (no audio). Pure."""
    real = [(spk, start) for spk, start, _ in runs if spk != _MARKER]
    out: list[tuple[str, float, float]] = []
    for i, (spk, start) in enumerate(real):
        end = real[i + 1][1] if i + 1 < len(real) else now_elapsed
        if end > start:
            out.append((spk, start, end))
    return out


@dataclass
class TranscriptAccumulator:
    title: str = "Active listen session"
    committed: list[tuple[str, float, str]] = field(default_factory=list)  # (speaker, start, text)
    partial: str = ""  # provisional trailing text, superseded by the next final
    # Speaker-ID display map: S-label -> resolved display name. Empty unless active speaker-ID is on
    # AND a speaker matched an enrolled voiceprint, so when off this is {} → raw S# (unchanged).
    labels: dict[str, str] = field(default_factory=dict)

    def add_final(self, msg: dict) -> None:
        self.partial = ""  # a final supersedes any in-flight partial
        for speaker, start, text in words_to_runs(msg.get("results")):
            if self.committed and self.committed[-1][0] == speaker:
                spk, start0, text0 = self.committed[-1]
                self.committed[-1] = (spk, start0, f"{text0} {text}")
            else:
                self.committed.append((speaker, start, text))

    def set_partial(self, msg: dict) -> None:
        md = msg.get("metadata") or {}
        self.partial = (md.get("transcript") or "").strip()

    def add_marker(self, elapsed_s: float) -> None:
        """Drop a timestamped user bookmark at ``elapsed_s`` (seconds into the session).
        Appended to the committed stream as a sentinel run → rendered as a divider line.
        Does NOT clear ``partial``: the in-flight provisional text is current speech that
        renders just AFTER the marker. The sentinel also breaks the speaker-merge chain in
        ``add_final`` (no real speaker equals it), so speech after the marker starts fresh —
        exactly the intent of bookmarking a point mid-utterance."""
        self.committed.append((_MARKER, float(elapsed_s), ""))

    def render(self) -> str:
        lines = [f"# {self.title}", ""]
        for speaker, start, text in self.committed:
            if speaker == _MARKER:
                lines.append(f"[{_fmt_ts(start)}] --- marker ---")
            else:
                display = self.labels.get(speaker, speaker)  # resolved name, else raw S#
                lines.append(f"[{_fmt_ts(start)}] **{display}**: {text.strip()}")
        if self.partial:
            lines.append(f"_… {self.partial}_")
        return "\n".join(lines) + "\n"
