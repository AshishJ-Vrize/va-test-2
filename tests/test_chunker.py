"""
Tests for app/services/ingestion/chunker.py

Covers:
  - merge_speaker_turns: same speaker within gap merges
  - merge_speaker_turns: different speaker never merges
  - merge_speaker_turns: same speaker beyond gap stays separate
  - merge_speaker_turns: overlapping timestamps (negative gap) merge
  - merge_speaker_turns: empty input
  - chunk_segments: short segment becomes single chunk
  - chunk_segments: long segment split into multiple chunks
  - chunk_segments: chunk_index is always sequential from 0
  - chunk_segments: end_ms always > start_ms (even with zero-duration segment)
  - chunk_segments: speaker and text are never empty
  - chunk_segments: empty input returns empty list
"""

import pytest

from app.services.ingestion.chunker import (
    MAX_WORDS_PER_CHUNK,
    MERGE_GAP_MS,
    Chunk,
    chunk_segments,
    merge_speaker_turns,
)
from app.services.ingestion.vtt_parser import VttSegment


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_seg(speaker: str, text: str, start_ms: int, end_ms: int) -> VttSegment:
    return VttSegment(speaker=speaker, text=text, start_ms=start_ms, end_ms=end_ms)


def long_text(n_words: int) -> str:
    return " ".join(f"word{i}" for i in range(n_words))


# ── merge_speaker_turns ───────────────────────────────────────────────────────

class TestMergeSpeakerTurns:
    def test_same_speaker_within_gap_merges(self):
        segs = [
            make_seg("John", "Hello", 0, 2_000),
            make_seg("John", "world", 3_000, 5_000),  # gap = 1000 ms ≤ 2000
        ]
        merged = merge_speaker_turns(segs)
        assert len(merged) == 1
        assert merged[0].text == "Hello world"
        assert merged[0].start_ms == 0
        assert merged[0].end_ms == 5_000

    def test_different_speaker_never_merges(self):
        segs = [
            make_seg("John", "Hello", 0, 2_000),
            make_seg("Jane", "Hi", 2_500, 4_000),
        ]
        merged = merge_speaker_turns(segs)
        assert len(merged) == 2

    def test_same_speaker_beyond_gap_stays_separate(self):
        gap = MERGE_GAP_MS + 1  # just over the threshold
        segs = [
            make_seg("John", "Part one", 0, 1_000),
            make_seg("John", "Part two", 1_000 + gap, 5_000),
        ]
        merged = merge_speaker_turns(segs)
        assert len(merged) == 2

    def test_same_speaker_exactly_at_gap_boundary_merges(self):
        segs = [
            make_seg("John", "A", 0, 1_000),
            make_seg("John", "B", 1_000 + MERGE_GAP_MS, 3_000),  # gap == MERGE_GAP_MS
        ]
        merged = merge_speaker_turns(segs)
        assert len(merged) == 1

    def test_overlapping_timestamps_same_speaker_merges(self):
        # negative gap (overlapping cues) — should still merge
        segs = [
            make_seg("John", "Alpha", 0, 3_000),
            make_seg("John", "Beta", 2_000, 5_000),  # gap = -1000 ms
        ]
        merged = merge_speaker_turns(segs)
        assert len(merged) == 1

    def test_start_ms_preserved_from_first_segment(self):
        segs = [
            make_seg("John", "A", 500, 1_500),
            make_seg("John", "B", 2_000, 3_000),
        ]
        merged = merge_speaker_turns(segs)
        assert merged[0].start_ms == 500

    def test_end_ms_taken_from_last_segment(self):
        segs = [
            make_seg("John", "A", 0, 1_000),
            make_seg("John", "B", 1_500, 4_000),
        ]
        merged = merge_speaker_turns(segs)
        assert merged[0].end_ms == 4_000

    def test_empty_input_returns_empty(self):
        assert merge_speaker_turns([]) == []

    def test_single_segment_returned_unchanged(self):
        segs = [make_seg("John", "Solo", 0, 5_000)]
        merged = merge_speaker_turns(segs)
        assert len(merged) == 1
        assert merged[0].text == "Solo"

    def test_input_list_not_mutated(self):
        segs = [
            make_seg("John", "A", 0, 1_000),
            make_seg("John", "B", 1_500, 2_500),
        ]
        original_len = len(segs)
        merge_speaker_turns(segs)
        assert len(segs) == original_len

    def test_three_way_merge(self):
        segs = [
            make_seg("John", "One", 0, 1_000),
            make_seg("John", "Two", 1_500, 2_500),
            make_seg("John", "Three", 3_000, 4_000),
        ]
        merged = merge_speaker_turns(segs)
        assert len(merged) == 1
        assert "One Two Three" == merged[0].text

    def test_alternating_speakers_no_merge(self):
        segs = [
            make_seg("A", "a1", 0, 1_000),
            make_seg("B", "b1", 1_100, 2_000),
            make_seg("A", "a2", 2_100, 3_000),
            make_seg("B", "b2", 3_100, 4_000),
        ]
        merged = merge_speaker_turns(segs)
        assert len(merged) == 4


# ── chunk_segments ────────────────────────────────────────────────────────────

class TestChunkSegments:
    def test_short_segment_becomes_one_chunk(self):
        segs = [make_seg("John", "Short text here", 0, 5_000)]
        chunks = chunk_segments(segs)
        assert len(chunks) == 1

    def test_short_segment_text_preserved(self):
        segs = [make_seg("John", "Short text here", 0, 5_000)]
        chunks = chunk_segments(segs)
        assert chunks[0].text == "Short text here"

    def test_short_segment_speaker_preserved(self):
        segs = [make_seg("John", "Hello", 0, 5_000)]
        chunks = chunk_segments(segs)
        assert chunks[0].speaker == "John"

    def test_short_segment_timestamps_preserved(self):
        segs = [make_seg("John", "Hello", 1_000, 4_000)]
        chunks = chunk_segments(segs)
        assert chunks[0].start_ms == 1_000
        assert chunks[0].end_ms == 4_000

    def test_long_segment_splits_into_multiple_chunks(self):
        text = long_text(MAX_WORDS_PER_CHUNK + 1)
        segs = [make_seg("John", text, 0, 60_000)]
        chunks = chunk_segments(segs)
        assert len(chunks) == 2

    def test_exactly_max_words_stays_one_chunk(self):
        text = long_text(MAX_WORDS_PER_CHUNK)
        segs = [make_seg("John", text, 0, 60_000)]
        chunks = chunk_segments(segs)
        assert len(chunks) == 1

    def test_chunk_index_sequential_from_zero(self):
        text = long_text(MAX_WORDS_PER_CHUNK * 3 + 1)
        segs = [make_seg("John", text, 0, 120_000)]
        chunks = chunk_segments(segs)
        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i

    def test_chunk_index_sequential_across_multiple_segments(self):
        segs = [
            make_seg("John", long_text(MAX_WORDS_PER_CHUNK + 1), 0, 60_000),
            make_seg("Jane", "Short text", 61_000, 65_000),
        ]
        chunks = chunk_segments(segs)
        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i

    def test_end_ms_always_greater_than_start_ms(self):
        # Even with zero-duration segments the guard must hold.
        segs = [make_seg("John", long_text(MAX_WORDS_PER_CHUNK + 1), 5_000, 5_000)]
        chunks = chunk_segments(segs)
        for chunk in chunks:
            assert chunk.end_ms > chunk.start_ms

    def test_no_empty_text_in_chunks(self):
        segs = [make_seg("John", long_text(MAX_WORDS_PER_CHUNK * 2 + 1), 0, 60_000)]
        chunks = chunk_segments(segs)
        for chunk in chunks:
            assert chunk.text.strip()

    def test_no_empty_speaker_in_chunks(self):
        segs = [make_seg("Unknown", long_text(MAX_WORDS_PER_CHUNK + 1), 0, 60_000)]
        chunks = chunk_segments(segs)
        for chunk in chunks:
            assert chunk.speaker

    def test_word_count_preserved_across_splits(self):
        """Total words in chunks must equal total words in input."""
        words_in = MAX_WORDS_PER_CHUNK * 2 + 50
        text = long_text(words_in)
        segs = [make_seg("John", text, 0, 60_000)]
        chunks = chunk_segments(segs)
        total_out = sum(len(c.text.split()) for c in chunks)
        assert total_out == words_in

    def test_empty_input_returns_empty_list(self):
        assert chunk_segments([]) == []

    def test_all_chunks_are_chunk_instances(self):
        segs = [make_seg("John", "Hello world", 0, 5_000)]
        chunks = chunk_segments(segs)
        for c in chunks:
            assert isinstance(c, Chunk)

    def test_speaker_carried_into_all_sub_chunks(self):
        text = long_text(MAX_WORDS_PER_CHUNK * 3)
        segs = [make_seg("Alice", text, 0, 90_000)]
        chunks = chunk_segments(segs)
        for chunk in chunks:
            assert chunk.speaker == "Alice"

    def test_timestamps_span_full_range(self):
        """First sub-chunk start == segment start; last sub-chunk end == segment end."""
        text = long_text(MAX_WORDS_PER_CHUNK * 2)
        segs = [make_seg("John", text, 1_000, 61_000)]
        chunks = chunk_segments(segs)
        assert chunks[0].start_ms == 1_000
        assert chunks[-1].end_ms == 61_000
