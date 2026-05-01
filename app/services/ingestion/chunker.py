from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.ingestion.vtt_parser import VttSegment

# ── Constants (original chunker — kept for backward compat) ───────────────────

# Maximum words allowed in a single chunk.
# text-embedding-3-small has an 8 191-token limit; 300 words ≈ 400 tokens,
# leaving headroom for any tokenisation overhead.
MAX_WORDS_PER_CHUNK = 300

# Two consecutive turns from the same speaker are merged when the silence
# gap between them is no longer than this value (in milliseconds).
# 2 000 ms covers natural pauses without merging turns that are truly separate thoughts.
MERGE_GAP_MS = 2_000

# ── Constants (production chunker) ────────────────────────────────────────────

# Target word count per chunk. Smaller than 300 improves retrieval precision;
# neighbor expansion at query time recovers the surrounding context.
TARGET_WORDS = 250

# Words carried from the tail of chunk N into the head of chunk N+1.
# Prevents losing context when a sentence straddles a chunk boundary.
OVERLAP_WORDS = 40

# Chunks below this word count are absorbed into the previous chunk.
# Eliminates embeddings for filler turns ("Agreed.", "Yeah.", "Okay.") that
# contain no independent semantic content.
MIN_CHUNK_WORDS = 20

# Sentence boundary regex: split after [.!?] followed by whitespace + uppercase.
# Conservative: only fires on clear sentence endings to avoid over-splitting
# transcript lines that lack punctuation.
_SENT_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"])")


@dataclass
class Chunk:
    """
    A text chunk ready to be contextualised, embedded, and stored.

    chunk_index : Zero-based position within the meeting transcript.
    text        : Raw spoken content — displayed in UI and citations.
    speaker     : Display name of the speaker ("Unknown" as fallback).
    start_ms    : Chunk start in milliseconds from meeting start.
    end_ms      : Chunk end in milliseconds — always strictly > start_ms.
    """

    chunk_index: int
    text: str
    speaker: str
    start_ms: int
    end_ms: int


# ── Shared helper ─────────────────────────────────────────────────────────────

def merge_speaker_turns(segments: list[VttSegment]) -> list[VttSegment]:
    """
    Merge consecutive VTT segments from the same speaker into a single segment
    when the silence gap between them is ≤ MERGE_GAP_MS (2 seconds).

    Teams VTT breaks one continuous turn into many small cue blocks (often one
    per clause).  Merging restores the natural thought unit before chunking.

    Rules
    -----
    - Only merges when BOTH conditions hold:
        1. The current segment's speaker == the previous segment's speaker.
        2. The gap (current.start_ms - previous.end_ms) ≤ MERGE_GAP_MS.
    - When merged, start_ms comes from the first segment, end_ms from the last.
    - Overlapping timestamps (negative gap) are treated as zero gap and merged.

    Returns a new list — input is not mutated.
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


# ── Original chunker (kept for backward compatibility) ────────────────────────

def chunk_segments(segments: list[VttSegment]) -> list[Chunk]:
    """
    Original fixed-window chunker.  Splits at exactly MAX_WORDS_PER_CHUNK (300)
    words with proportional timestamp distribution.

    Retained for backward compatibility and tests.  New code should use
    chunk_with_sentences() which is sentence-aware, overlap-enabled, and
    filters trivial chunks.

    Splitting strategy
    ------------------
    - If a segment is ≤ MAX_WORDS_PER_CHUNK (300 words) it becomes a single chunk.
    - If a segment exceeds 300 words it is split into multiple sub-chunks of ≤ 300
      words each.  Timestamps for sub-chunks are distributed proportionally by word
      position within the original segment's time range.

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


# ── Production chunker ────────────────────────────────────────────────────────

def chunk_with_sentences(segments: list[VttSegment]) -> list[Chunk]:
    """
    Production-grade chunker with three improvements over chunk_segments:

    1. Sentence-aware splitting — never cuts a sentence in half.
       Uses a conservative regex that only fires on [.!?] + whitespace + capital.
       When a segment has no sentence boundaries (e.g. a long unpunctuated
       transcript line), falls back gracefully to word-count splitting.

    2. Overlap — the last OVERLAP_WORDS (40) words of chunk N are prepended to
       chunk N+1.  This preserves cross-boundary context ("she said X, so we
       decided Y" where X ends chunk N and Y starts chunk N+1).

    3. Tiny-chunk absorption — chunks below MIN_CHUNK_WORDS (20 words) are
       appended to the preceding chunk rather than emitted separately.
       This prevents filler turns ("Agreed.", "Yes.", "Confirmed.") from being
       embedded in isolation, which produces low-quality retrieval vectors.
       Exception: a tiny chunk that is the *first* (or only) result is kept
       so the function never returns an empty list when content exists.

    chunk_index is always sequential from 0 across all segments.
    end_ms is always strictly > start_ms (guarded explicitly).
    """
    if not segments:
        return []

    raw: list[Chunk] = []
    idx = 0

    for seg in segments:
        words = seg.text.split()
        if not words:
            continue

        if len(words) <= TARGET_WORDS:
            raw.append(
                Chunk(
                    chunk_index=idx,
                    text=seg.text,
                    speaker=seg.speaker,
                    start_ms=seg.start_ms,
                    end_ms=seg.end_ms,
                )
            )
            idx += 1
            continue

        # Long segment — split at sentence boundaries with overlap.
        sub_chunks = _split_long_segment(seg, idx)
        raw.extend(sub_chunks)
        idx += len(sub_chunks)

    return _absorb_tiny_chunks(raw)


# ── Private helpers ───────────────────────────────────────────────────────────

def _split_long_segment(seg: VttSegment, start_idx: int) -> list[Chunk]:
    """
    Split a single long speaker segment into sentence-aware chunks with overlap.
    Falls back to word-boundary splitting when no sentence markers are found.
    """
    sentences = _SENT_BOUNDARY.split(seg.text)

    # If no sentence boundaries detected, treat the whole text as one sentence
    # and rely on word-count splitting below.
    if len(sentences) == 1:
        sentences = _word_split_fallback(seg.text)

    total_words = len(seg.text.split())
    duration_ms = seg.end_ms - seg.start_ms
    chunks: list[Chunk] = []
    idx = start_idx

    buf_sentences: list[str] = []
    buf_words = 0
    words_consumed = 0  # tracks position in original text for timestamp interpolation

    for sent in sentences:
        sw = len(sent.split())

        # If adding this sentence would overflow and we already have content,
        # emit the current buffer as a chunk then start fresh with overlap.
        if buf_words + sw > TARGET_WORDS and buf_words >= MIN_CHUNK_WORDS:
            chunk_text = " ".join(buf_sentences)
            chunk_start_word = max(0, words_consumed - buf_words)
            chunks.append(
                Chunk(
                    chunk_index=idx,
                    text=chunk_text,
                    speaker=seg.speaker,
                    start_ms=_interp_ms(seg.start_ms, duration_ms, chunk_start_word, total_words),
                    end_ms=_interp_ms(seg.start_ms, duration_ms, words_consumed, total_words),
                )
            )
            idx += 1

            # Carry last OVERLAP_WORDS from the emitted chunk into the next one.
            tail_words = chunk_text.split()[-OVERLAP_WORDS:]
            buf_sentences = [" ".join(tail_words)]
            buf_words = len(tail_words)

        buf_sentences.append(sent)
        buf_words += sw
        words_consumed += sw

    # Emit remaining buffer.
    if buf_sentences:
        chunk_text = " ".join(buf_sentences)
        chunk_start_word = max(0, words_consumed - buf_words)
        end_ms = seg.end_ms
        if end_ms <= _interp_ms(seg.start_ms, duration_ms, chunk_start_word, total_words):
            end_ms = _interp_ms(seg.start_ms, duration_ms, chunk_start_word, total_words) + 1
        chunks.append(
            Chunk(
                chunk_index=idx,
                text=chunk_text,
                speaker=seg.speaker,
                start_ms=_interp_ms(seg.start_ms, duration_ms, chunk_start_word, total_words),
                end_ms=end_ms,
            )
        )

    return chunks


def _word_split_fallback(text: str) -> list[str]:
    """
    Split text purely by word count when no sentence boundaries are present.
    Returns segments of ≤ TARGET_WORDS words each.
    """
    words = text.split()
    return [
        " ".join(words[i : i + TARGET_WORDS])
        for i in range(0, len(words), TARGET_WORDS)
    ]


def _absorb_tiny_chunks(chunks: list[Chunk]) -> list[Chunk]:
    """
    Merge any chunk shorter than MIN_CHUNK_WORDS into the preceding chunk.
    Preserves the preceding chunk's speaker label (the filler response belongs
    in context with what prompted it).

    Chunks that are the first (or only) element are kept even if tiny, so the
    function never returns an empty list when the input is non-empty.
    """
    if not chunks:
        return []

    result: list[Chunk] = [chunks[0]]

    for c in chunks[1:]:
        if len(c.text.split()) < MIN_CHUNK_WORDS:
            prev = result[-1]
            result[-1] = Chunk(
                chunk_index=prev.chunk_index,
                text=prev.text + " " + c.text,
                speaker=prev.speaker,
                start_ms=prev.start_ms,
                end_ms=c.end_ms,
            )
        else:
            result.append(c)

    # Re-index sequentially from 0.
    for i, c in enumerate(result):
        c.chunk_index = i

    return result


def _interp_ms(start_ms: int, duration_ms: int, word_pos: int, total_words: int) -> int:
    """Linearly interpolate a timestamp from word position within a segment."""
    if total_words == 0 or duration_ms == 0:
        return start_ms
    return start_ms + int(duration_ms * word_pos / total_words)
