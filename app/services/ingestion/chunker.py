from __future__ import annotations

from dataclasses import dataclass

from app.services.ingestion.vtt_parser import VttSegment

# Maximum words allowed in a single chunk.
# text-embedding-3-small has an 8 191-token limit; 300 words ≈ 400 tokens,
# leaving headroom for any tokenisation overhead.
MAX_WORDS_PER_CHUNK = 300

# Two consecutive turns from the same speaker are merged when the silence
# gap between them is no longer than this value (in milliseconds).
# 2 000 ms covers natural pauses without merging turns that are truly separate thoughts.
MERGE_GAP_MS = 2_000


@dataclass
class Chunk:
    """
    A fixed-size text chunk ready to be embedded and stored in the chunks table.

    Fields
    ------
    chunk_index : Zero-based position within the meeting transcript (used for ordering).
    text        : Spoken content — never empty.
    speaker     : Display name of the speaker — never empty ("Unknown" as fallback).
    start_ms    : Start of this chunk in milliseconds from the beginning of the meeting.
    end_ms      : End of this chunk in milliseconds — always strictly > start_ms.
    """

    chunk_index: int
    text: str
    speaker: str
    start_ms: int
    end_ms: int


def merge_speaker_turns(segments: list[VttSegment]) -> list[VttSegment]:
    """
    Merge consecutive VTT segments from the same speaker into a single segment
    when the silence gap between them is ≤ MERGE_GAP_MS (2 seconds).

    Why this is needed
    ------------------
    Teams VTT breaks a continuous speaker turn into many small cue blocks
    (often one per sentence or even per clause).  Embedding tiny 3–5 word
    fragments produces poor vector representations.  Merging restores the
    natural thought unit before chunking.

    Rules
    -----
    - Only merges when BOTH conditions hold:
        1. The current segment's speaker == the previous segment's speaker.
        2. The gap (current.start_ms - previous.end_ms) ≤ MERGE_GAP_MS.
    - When merged, start_ms comes from the first segment, end_ms from the last.
    - Overlapping timestamps (negative gap) are treated as zero-gap and merged.

    Returns a new list — the input list is not mutated.
    """
    if not segments:
        return []

    # Initialise with a copy of the first segment so the input is never mutated.
    merged: list[VttSegment] = [
        VttSegment(
            speaker=segments[0].speaker,
            text=segments[0].text,
            start_ms=segments[0].start_ms,
            end_ms=segments[0].end_ms,
        )
    ]

    for seg in segments[1:]:
        prev = merged[-1]
        gap_ms = seg.start_ms - prev.end_ms  # negative = overlapping timestamps

        if seg.speaker == prev.speaker and gap_ms <= MERGE_GAP_MS:
            # Extend the previous segment to include this one.
            merged[-1] = VttSegment(
                speaker=prev.speaker,
                text=prev.text + " " + seg.text,
                start_ms=prev.start_ms,  # keep original start
                end_ms=seg.end_ms,       # extend end to this segment's end
            )
        else:
            # Different speaker or gap too large — start a new segment.
            merged.append(
                VttSegment(
                    speaker=seg.speaker,
                    text=seg.text,
                    start_ms=seg.start_ms,
                    end_ms=seg.end_ms,
                )
            )

    return merged


def chunk_segments(segments: list[VttSegment]) -> list[Chunk]:
    """
    Convert merged speaker segments into fixed-size Chunk objects ready for embedding.

    Splitting strategy
    ------------------
    - If a segment is ≤ MAX_WORDS_PER_CHUNK (300 words) it becomes a single chunk.
    - If a segment exceeds 300 words it is split into multiple sub-chunks of ≤ 300
      words each.  Timestamps for sub-chunks are distributed proportionally by word
      position within the original segment's time range.

    Timestamp distribution example
    --------------------------------
    Segment: speaker=John, 600 words, 0 ms → 60 000 ms
      Sub-chunk 0: words   1–300, start=    0 ms, end=30 000 ms  (50% of duration)
      Sub-chunk 1: words 301–600, start=30 000 ms, end=60 000 ms

    Guarantees
    ----------
    Every returned Chunk has all 5 fields populated:
      - chunk_index : monotonically increasing from 0, no gaps
      - text        : non-empty string
      - speaker     : non-empty string
      - start_ms    : integer ≥ 0
      - end_ms      : always strictly > start_ms (guarded explicitly)
    """
    chunks: list[Chunk] = []
    idx = 0  # global chunk counter across all segments

    for seg in segments:
        words = seg.text.split()
        if not words:
            continue  # Empty segment after merge — skip defensively.

        if len(words) <= MAX_WORDS_PER_CHUNK:
            # Segment fits in one chunk — use it as-is.
            chunks.append(
                Chunk(
                    chunk_index=idx,
                    text=seg.text,
                    speaker=seg.speaker,
                    start_ms=seg.start_ms,
                    end_ms=seg.end_ms,
                )
            )
            idx += 1

        else:
            # Segment is too long — split and distribute timestamps proportionally.
            total_words = len(words)
            duration_ms = seg.end_ms - seg.start_ms
            start_word = 0

            while start_word < total_words:
                end_word = min(start_word + MAX_WORDS_PER_CHUNK, total_words)
                chunk_text = " ".join(words[start_word:end_word])

                # Proportional timestamp: fraction of words elapsed maps to
                # fraction of the segment's duration elapsed.
                chunk_start_ms = seg.start_ms + int(
                    duration_ms * start_word / total_words
                )
                chunk_end_ms = seg.start_ms + int(
                    duration_ms * end_word / total_words
                )

                # Safety guard: end must always be strictly after start.
                # Can occur when duration_ms is 0 (malformed VTT timestamps).
                if chunk_end_ms <= chunk_start_ms:
                    chunk_end_ms = chunk_start_ms + 1

                chunks.append(
                    Chunk(
                        chunk_index=idx,
                        text=chunk_text,
                        speaker=seg.speaker,
                        start_ms=chunk_start_ms,
                        end_ms=chunk_end_ms,
                    )
                )
                idx += 1
                start_word = end_word

    return chunks
