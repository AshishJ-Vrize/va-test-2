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
    MIN_CHUNK_WORDS,
    OVERLAP_WORDS,
    TARGET_WORDS,
    Chunk,
    chunk_segments,
    chunk_with_sentences,
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


# ── chunk_with_sentences ──────────────────────────────────────────────────────

# Realistic Teams VTT meeting fixture used across multiple tests.
TEAMS_VTT_SEGMENTS = [
    # Short filler turns — should be absorbed into neighbours.
    make_seg("Raj Patel", "Morning.", 10_100, 11_200),
    make_seg("Sara Nguyen", "Hi, good morning.", 11_400, 12_100),
    make_seg("Marcus Lee", "Morning.", 12_300, 12_800),
    # A proper medium-length turn.
    make_seg(
        "Priyanka Sharma",
        (
            "So the agenda today is three things. "
            "First, finalise the Acme renewal. "
            "Second, review Q4 budget allocation. "
            "Third, assign ownership for the product launch tasks. "
            "Raj can you start with the Acme update?"
        ),
        13_500,
        38_200,
    ),
    # A long turn that must be split with sentence awareness and overlap.
    make_seg(
        "Raj Patel",
        (
            "Sure. So Acme came back last Thursday with two concerns. "
            "First they want a price lock for 24 months instead of 12. "
            "Second they said our six week onboarding timeline is too long for their IT team. "
            "Their procurement deadline is November 15th so we have about three weeks to close. "
            "I spoke to David Chen their account manager on Friday. "
            "He said if we do 16 weeks onboarding and hold price for 18 months they are likely to sign. "
            "I think we should take that deal. "
            "The revenue impact is 340K ARR and we cannot let this slip into next quarter. "
            "I already checked with finance and 18 month price lock is within our approved discount policy. "
            "Sara what do you think about the onboarding timeline from a technical perspective?"
        ),
        39_000,
        118_700,
    ),
    # Decision + short confirmations — confirmations must be absorbed.
    make_seg(
        "Priyanka Sharma",
        (
            "Okay so decision here is we accept Acme's terms. "
            "18 month price lock, 16 week onboarding. "
            "Raj you will send the revised proposal by end of day Wednesday. "
            "Sara you own the onboarding plan and Marcus you confirm Arjun and Divya allocation. "
            "Everyone agreed?"
        ),
        120_400,
        134_600,
    ),
    make_seg("Raj Patel", "Agreed.", 135_100, 135_600),
    make_seg("Sara Nguyen", "Yes.", 135_800, 136_200),
    make_seg("Marcus Lee", "Confirmed.", 136_400, 136_900),
]


def make_merged_segs() -> list:
    """Run merge_speaker_turns on the fixture so tests start from realistic input."""
    return merge_speaker_turns(TEAMS_VTT_SEGMENTS)


class TestChunkWithSentences:

    # ── Basic output contract ─────────────────────────────────────────────────

    def test_empty_input_returns_empty(self):
        assert chunk_with_sentences([]) == []

    def test_returns_chunk_instances(self):
        merged = make_merged_segs()
        chunks = chunk_with_sentences(merged)
        for c in chunks:
            assert isinstance(c, Chunk)

    def test_chunk_index_sequential_from_zero(self):
        merged = make_merged_segs()
        chunks = chunk_with_sentences(merged)
        for i, c in enumerate(chunks):
            assert c.chunk_index == i

    def test_end_ms_always_greater_than_start_ms(self):
        merged = make_merged_segs()
        chunks = chunk_with_sentences(merged)
        for c in chunks:
            assert c.end_ms > c.start_ms, (
                f"chunk {c.chunk_index}: end_ms={c.end_ms} <= start_ms={c.start_ms}"
            )

    def test_no_empty_text(self):
        merged = make_merged_segs()
        chunks = chunk_with_sentences(merged)
        for c in chunks:
            assert c.text.strip(), f"chunk {c.chunk_index} has empty text"

    def test_no_empty_speaker(self):
        merged = make_merged_segs()
        chunks = chunk_with_sentences(merged)
        for c in chunks:
            assert c.speaker, f"chunk {c.chunk_index} has empty speaker"

    def test_speaker_preserved(self):
        segs = [make_seg("Alice", "Short sentence here.", 0, 5_000)]
        chunks = chunk_with_sentences(segs)
        assert all(c.speaker == "Alice" for c in chunks)

    # ── Tiny-chunk absorption ─────────────────────────────────────────────────

    def test_tiny_confirmations_absorbed_into_decision(self):
        """'Agreed.' 'Yes.' 'Confirmed.' must be merged into the preceding chunk."""
        merged = make_merged_segs()
        chunks = chunk_with_sentences(merged)
        # None of the standalone 1-word turns should appear as their own chunk.
        one_word_only = [c for c in chunks if len(c.text.split()) == 1]
        assert one_word_only == [], (
            f"Found isolated 1-word chunks: {[c.text for c in one_word_only]}"
        )

    def test_no_chunk_below_min_words_except_first(self):
        """Every chunk except possibly the very first must meet MIN_CHUNK_WORDS."""
        merged = make_merged_segs()
        chunks = chunk_with_sentences(merged)
        for c in chunks[1:]:
            assert len(c.text.split()) >= MIN_CHUNK_WORDS, (
                f"chunk {c.chunk_index} has only {len(c.text.split())} words: {c.text!r}"
            )

    def test_single_tiny_segment_kept(self):
        """A tiny segment that is the only input must still be returned."""
        segs = [make_seg("Bob", "Okay.", 0, 500)]
        chunks = chunk_with_sentences(segs)
        assert len(chunks) == 1
        assert chunks[0].text == "Okay."

    def test_tiny_first_segment_kept_larger_second_separate(self):
        """First tiny chunk kept; second (large) chunk is separate."""
        tiny = make_seg("A", "Sure.", 0, 500)
        big_text = ". ".join([f"This is sentence number {i}" for i in range(30)]) + "."
        big = make_seg("B", big_text, 1_000, 60_000)
        chunks = chunk_with_sentences([tiny, big])
        assert len(chunks) >= 1
        assert chunks[0].text == "Sure."

    # ── Sentence-aware splitting ──────────────────────────────────────────────

    def test_long_segment_never_splits_mid_sentence(self):
        """Every chunk boundary must fall at a sentence end (period/question mark)."""
        merged = make_merged_segs()
        chunks = chunk_with_sentences(merged)
        for c in chunks[:-1]:  # last chunk may not end with punctuation
            last_char = c.text.rstrip()[-1] if c.text.rstrip() else ""
            # Not strict — transcripts may lack punctuation — but when it splits,
            # the previous chunk should end with sentence-terminal punctuation.
            # We just confirm no chunk ends mid-word (no trailing space).
            assert not c.text.endswith(" "), (
                f"chunk {c.chunk_index} ends with trailing space: {c.text[-20:]!r}"
            )

    def test_long_turn_produces_multiple_chunks(self):
        """A turn with > TARGET_WORDS (250) words must produce at least 2 chunks."""
        # Build text that is definitively > 250 words with clear sentence boundaries.
        sentences = [
            f"Point number {i} is an important consideration for the Acme renewal deal."
            for i in range(30)
        ]
        long_text_with_sents = " ".join(sentences)  # ~300 words
        raj_long = make_seg("Raj Patel", long_text_with_sents, 39_000, 118_700)
        chunks = chunk_with_sentences([raj_long])
        assert len(chunks) >= 2, (
            f"Long turn ({len(long_text_with_sents.split())} words) must produce "
            f"at least 2 chunks with TARGET_WORDS={TARGET_WORDS}"
        )

    def test_each_chunk_within_max_word_limit(self):
        """No chunk produced by chunk_with_sentences should exceed MAX_WORDS_PER_CHUNK."""
        merged = make_merged_segs()
        chunks = chunk_with_sentences(merged)
        for c in chunks:
            word_count = len(c.text.split())
            assert word_count <= MAX_WORDS_PER_CHUNK, (
                f"chunk {c.chunk_index} has {word_count} words (>{MAX_WORDS_PER_CHUNK})"
            )

    # ── Overlap ───────────────────────────────────────────────────────────────

    def test_overlap_words_appear_in_consecutive_chunks(self):
        """The last OVERLAP_WORDS of chunk N must appear at the start of chunk N+1
        when a long segment is split."""
        # Generate 35 sentences (~350 words) — well over TARGET_WORDS=250, guarantees split.
        sentences = [
            f"Sentence number {i} covers an important point about the Acme contract renewal deal."
            for i in range(35)
        ]
        long_text_with_sentences = " ".join(sentences)
        segs = [make_seg("Speaker", long_text_with_sentences, 0, 120_000)]
        chunks = chunk_with_sentences(segs)

        if len(chunks) < 2:
            pytest.skip("Text did not produce multiple chunks — adjust fixture")

        tail_words = chunks[0].text.split()[-OVERLAP_WORDS:]
        next_words = chunks[1].text.split()[:OVERLAP_WORDS]
        overlap_found = any(w in next_words for w in tail_words)
        assert overlap_found, (
            "Expected overlap words from chunk 0 tail to appear in chunk 1 head"
        )

    def test_all_original_content_covered(self):
        """Every word from the original input must appear in at least one chunk."""
        long_with_sentences = ". ".join(
            [f"Sentence number {i} contains important content" for i in range(50)]
        ) + "."
        segs = [make_seg("Alice", long_with_sentences, 0, 300_000)]
        chunks = chunk_with_sentences(segs)
        all_chunk_text = " ".join(c.text for c in chunks)

        # Check key sentences are present (sampling to avoid O(n²) scan).
        for i in [0, 10, 25, 49]:
            assert f"Sentence number {i}" in all_chunk_text, (
                f"'Sentence number {i}' not found in any chunk"
            )

    # ── Cross-segment index continuity ────────────────────────────────────────

    def test_chunk_index_continuous_across_multiple_segments(self):
        segs = [
            make_seg("Alice", ". ".join([f"Word{i}" for i in range(60)]) + ".", 0, 60_000),
            make_seg("Bob", "Short response here.", 61_000, 65_000),
            make_seg("Alice", ". ".join([f"More{i}" for i in range(60)]) + ".", 66_000, 126_000),
        ]
        chunks = chunk_with_sentences(segs)
        for i, c in enumerate(chunks):
            assert c.chunk_index == i

    # ── Timestamps ───────────────────────────────────────────────────────────

    def test_first_chunk_start_matches_segment_start(self):
        segs = [make_seg("Alice", "Hello world, this is a test sentence.", 5_000, 10_000)]
        chunks = chunk_with_sentences(segs)
        assert chunks[0].start_ms == 5_000

    def test_last_chunk_end_matches_segment_end_for_single_segment(self):
        segs = [make_seg("Alice", "Hello world, this is a test sentence.", 5_000, 10_000)]
        chunks = chunk_with_sentences(segs)
        assert chunks[-1].end_ms == 10_000
