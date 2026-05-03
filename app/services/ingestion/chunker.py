"""Multi-turn chunker — group merged speaker segments into chunks of 5-15
utterances spanning ~20-60 seconds.

A chunk holds multiple consecutive utterances from possibly multiple speakers,
preserving order. Each utterance carries speaker name (full + short), text,
and start/end seconds.

Boundary rules
--------------
HARD CAP — emit immediately when any of:
    * utterance count >= MAX_UTTERANCES (15)
    * span_sec >= MAX_SPAN_SEC (60)
    * word count >= MAX_WORDS (400)

PAUSE BREAK — emit when gap to next utterance >= PAUSE_GAP_SEC (8)
    AND chunk has reached SOFT_MIN_UTTERANCES (5)
    AND span_sec >= SOFT_MIN_SPAN_SEC (20)

END OF STREAM — always emit remaining buffer.

The dataclass `Chunk` is the in-memory representation between chunker and
pipeline. Its fields map 1:1 to columns on the chunks table except for
`meeting_id` (the pipeline injects that at INSERT time).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.services.ingestion.speaker_resolver import ResolvedSpeaker
from app.services.ingestion.vtt_parser import VttSegment

# Same-speaker silence-merge gap. Consecutive cues from one speaker within
# this window are treated as one logical turn before chunking begins.
MERGE_GAP_MS = 2_000

# Chunk size constraints.
MAX_UTTERANCES = 15
MAX_SPAN_SEC = 60
MAX_WORDS = 400
SOFT_MIN_UTTERANCES = 5
SOFT_MIN_SPAN_SEC = 20
PAUSE_GAP_SEC = 8


@dataclass
class Chunk:
    """One multi-turn chunk ready for persistence and embedding.

    speakers          : Deduped first-name list in display order
                        (e.g. ["Ashish", "Rahul"]).
    speaker_graph_ids : Deduped non-null graph IDs that spoke in this chunk.
                        NOT aligned with `speakers` — it's a queryable index
                        for cross-meeting per-person filtering.
    chunk_text        : Ordered list of utterance dicts:
                        {n, sn, t, st (sec), et (sec)}.
    """
    chunk_index: int
    start_ms: int
    end_ms: int
    speakers: list[str] = field(default_factory=list)
    speaker_graph_ids: list[str] = field(default_factory=list)
    chunk_text: list[dict] = field(default_factory=list)


def merge_speaker_turns(segments: list[VttSegment]) -> list[VttSegment]:
    """Merge consecutive same-speaker cues within MERGE_GAP_MS into one turn.

    Teams VTT splits one continuous turn into many small cues; this rebuilds
    the natural thought unit before chunking. Returns a new list — input
    segments are not mutated.
    """
    if not segments:
        return []

    merged: list[VttSegment] = [VttSegment(
        speaker=segments[0].speaker,
        text=segments[0].text,
        start_ms=segments[0].start_ms,
        end_ms=segments[0].end_ms,
    )]
    for seg in segments[1:]:
        prev = merged[-1]
        gap = seg.start_ms - prev.end_ms
        if seg.speaker == prev.speaker and gap <= MERGE_GAP_MS:
            merged[-1] = VttSegment(
                speaker=prev.speaker,
                text=prev.text + " " + seg.text,
                start_ms=prev.start_ms,
                end_ms=seg.end_ms,
            )
        else:
            merged.append(VttSegment(
                speaker=seg.speaker,
                text=seg.text,
                start_ms=seg.start_ms,
                end_ms=seg.end_ms,
            ))
    return merged


def chunk_segments(
    merged_segments: list[VttSegment],
    resolution: dict[str, ResolvedSpeaker],
) -> list[Chunk]:
    """Group merged speaker turns into multi-turn chunks.

    Args:
        merged_segments: output of merge_speaker_turns()
        resolution:      VTT speaker label → ResolvedSpeaker
                         (from speaker_resolver.build_speaker_resolution)

    Returns:
        Ordered list of Chunk objects with chunk_index 0..N-1.
    """
    if not merged_segments:
        return []

    chunks: list[Chunk] = []
    buf: list[VttSegment] = []
    buf_words = 0

    def emit():
        nonlocal buf, buf_words
        if buf:
            chunks.append(_build_chunk(buf, resolution, len(chunks)))
            buf = []
            buf_words = 0

    for i, seg in enumerate(merged_segments):
        buf.append(seg)
        buf_words += len(seg.text.split())
        span_sec = (buf[-1].end_ms - buf[0].start_ms) // 1000

        # Hard caps — force emit regardless of soft minima.
        if (
            len(buf) >= MAX_UTTERANCES
            or span_sec >= MAX_SPAN_SEC
            or buf_words >= MAX_WORDS
        ):
            emit()
            continue

        # Pause break — emit only when soft floor is met.
        if i + 1 < len(merged_segments):
            next_gap_sec = (merged_segments[i + 1].start_ms - seg.end_ms) // 1000
            if (
                next_gap_sec >= PAUSE_GAP_SEC
                and len(buf) >= SOFT_MIN_UTTERANCES
                and span_sec >= SOFT_MIN_SPAN_SEC
            ):
                emit()

    emit()
    return chunks


def _build_chunk(
    segs: list[VttSegment],
    resolution: dict[str, ResolvedSpeaker],
    chunk_index: int,
) -> Chunk:
    """Assemble a Chunk from its constituent merged segments + speaker map."""
    chunk_text: list[dict] = []
    seen_sn: list[str] = []          # ordered, deduped — display order
    seen_gids: list[str] = []        # ordered, deduped non-null graph IDs

    for seg in segs:
        rs = resolution.get(seg.speaker)
        if rs is None:
            # Defensive: speaker not in resolution map (shouldn't happen if
            # caller built the map from this meeting's segments).
            n = seg.speaker or "Unknown"
            sn = (n.split() or [n])[0]
            graph_id: str | None = None
        else:
            n, sn, graph_id = rs.n, rs.sn, rs.graph_id

        chunk_text.append({
            "n": n,
            "sn": sn,
            "t": seg.text,
            "st": seg.start_ms // 1000,
            "et": seg.end_ms // 1000,
        })

        if sn not in seen_sn:
            seen_sn.append(sn)
        if graph_id and graph_id not in seen_gids:
            seen_gids.append(graph_id)

    return Chunk(
        chunk_index=chunk_index,
        start_ms=segs[0].start_ms,
        end_ms=segs[-1].end_ms,
        speakers=seen_sn,
        speaker_graph_ids=seen_gids,
        chunk_text=chunk_text,
    )
