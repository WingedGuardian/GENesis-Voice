"""Diarized transcript accumulator — pure + testable (dict in, str out).

Consumes Speechmatics realtime ``AddTranscript`` (final) and
``AddPartialTranscript`` (provisional) message dicts and renders a live-updating,
speaker-labelled markdown transcript. No I/O, no SDK types — so it unit-tests
with hand-built message dicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def _fmt_ts(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def words_to_runs(results: list | None) -> list[tuple[str, float, str]]:
    """Group a Speechmatics ``results`` list (words/punctuation) into consecutive
    same-speaker runs → ``[(speaker, start_time, text), ...]``. Punctuation attaches
    to the preceding word with no leading space."""
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
            # Attach to the current run with no leading space; leading punctuation
            # (no preceding word) is dropped rather than starting a bogus run.
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


@dataclass
class TranscriptAccumulator:
    title: str = "Listen session"
    committed: list[tuple[str, float, str]] = field(default_factory=list)  # (speaker, start, text)
    partial: str = ""  # provisional trailing text, superseded by the next final

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

    def render(self) -> str:
        lines = [f"# {self.title}", ""]
        for speaker, start, text in self.committed:
            lines.append(f"[{_fmt_ts(start)}] **{speaker}**: {text.strip()}")
        if self.partial:
            lines.append(f"_… {self.partial}_")
        return "\n".join(lines) + "\n"
