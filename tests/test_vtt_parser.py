"""
Tests for app/services/ingestion/vtt_parser.py

Covers:
  - Standard Teams VTT format (HH:MM:SS.mmm)
  - Short-meeting format (MM:SS.mmm)
  - Cues with numeric cue IDs before the timestamp
  - Multi-line cue text
  - Cues with no <v> speaker tag (fallback to "Unknown")
  - HTML/VTT inline tags stripped from text
  - Empty cues discarded
  - WEBVTT / NOTE / STYLE / REGION blocks skipped
  - Windows line endings (CRLF) normalised
  - _ts_to_ms helper directly
"""

import pytest

from app.services.ingestion.vtt_parser import VttSegment, _ts_to_ms, parse_vtt


# ── _ts_to_ms ────────────────────────────────────────────────────────────────

class TestTsToMs:
    def test_full_hms_format(self):
        assert _ts_to_ms("00:00:01.000") == 1_000

    def test_full_hms_with_hours(self):
        assert _ts_to_ms("01:00:00.000") == 3_600_000

    def test_full_hms_fractional(self):
        assert _ts_to_ms("00:00:01.500") == 1_500

    def test_short_ms_format(self):
        # MM:SS.mmm — used by Teams for short meetings
        assert _ts_to_ms("01:30.000") == 90_000

    def test_hours_minutes_seconds_combined(self):
        assert _ts_to_ms("01:23:45.678") == (
            1 * 3600 * 1000 + 23 * 60 * 1000 + 45 * 1000 + 678
        )


# ── parse_vtt ────────────────────────────────────────────────────────────────

SIMPLE_VTT = """\
WEBVTT

00:00:01.000 --> 00:00:04.000
<v John Doe>Hello everyone, can you hear me?

00:00:05.000 --> 00:00:08.000
<v Jane Smith>Yes, loud and clear.
"""

class TestParseVttBasic:
    def test_returns_two_segments(self):
        segs = parse_vtt(SIMPLE_VTT)
        assert len(segs) == 2

    def test_first_segment_speaker(self):
        segs = parse_vtt(SIMPLE_VTT)
        assert segs[0].speaker == "John Doe"

    def test_first_segment_text(self):
        segs = parse_vtt(SIMPLE_VTT)
        assert segs[0].text == "Hello everyone, can you hear me?"

    def test_first_segment_timestamps(self):
        segs = parse_vtt(SIMPLE_VTT)
        assert segs[0].start_ms == 1_000
        assert segs[0].end_ms == 4_000

    def test_second_segment_speaker(self):
        segs = parse_vtt(SIMPLE_VTT)
        assert segs[1].speaker == "Jane Smith"

    def test_second_segment_timestamps(self):
        segs = parse_vtt(SIMPLE_VTT)
        assert segs[1].start_ms == 5_000
        assert segs[1].end_ms == 8_000


class TestParseVttCueIds:
    """Teams VTT often includes numeric cue IDs before the timestamp line."""

    VTT = """\
WEBVTT

1
00:00:01.000 --> 00:00:03.000
<v Alice>First line.

2
00:00:04.000 --> 00:00:06.000
<v Bob>Second line.
"""

    def test_cue_ids_are_ignored(self):
        segs = parse_vtt(self.VTT)
        assert len(segs) == 2

    def test_speakers_correct_with_cue_ids(self):
        segs = parse_vtt(self.VTT)
        assert segs[0].speaker == "Alice"
        assert segs[1].speaker == "Bob"


class TestParseVttNoSpeakerTag:
    """Cues without a <v> tag should fall back to speaker='Unknown'."""

    VTT = """\
WEBVTT

00:00:01.000 --> 00:00:03.000
Some spoken text without a speaker tag.
"""

    def test_fallback_speaker(self):
        segs = parse_vtt(self.VTT)
        assert len(segs) == 1
        assert segs[0].speaker == "Unknown"

    def test_text_preserved(self):
        segs = parse_vtt(self.VTT)
        assert segs[0].text == "Some spoken text without a speaker tag."


class TestParseVttInlineTags:
    """HTML / VTT inline tags must be stripped from the spoken text."""

    VTT = """\
WEBVTT

00:00:01.000 --> 00:00:04.000
<v John><b>Bold text</b> and <i>italic</i> words.
"""

    def test_tags_stripped(self):
        segs = parse_vtt(self.VTT)
        assert "<b>" not in segs[0].text
        assert "<i>" not in segs[0].text

    def test_text_content_preserved(self):
        segs = parse_vtt(self.VTT)
        assert "Bold text" in segs[0].text
        assert "italic" in segs[0].text


class TestParseVttEmptyCues:
    """Cues that contain only tags and produce empty text must be discarded."""

    VTT = """\
WEBVTT

00:00:01.000 --> 00:00:02.000
<v John><00:00:01.000>

00:00:03.000 --> 00:00:05.000
<v Jane>Real content here.
"""

    def test_empty_cue_discarded(self):
        segs = parse_vtt(self.VTT)
        assert len(segs) == 1
        assert segs[0].speaker == "Jane"


class TestParseVttSkipsHeaders:
    """WEBVTT header, NOTE, STYLE, REGION blocks must all be skipped."""

    VTT = """\
WEBVTT

NOTE This is a comment block

STYLE
::cue { font-size: 1em; }

REGION
id:r1

00:00:01.000 --> 00:00:03.000
<v Speaker>Actual content.
"""

    def test_only_cue_segment_returned(self):
        segs = parse_vtt(self.VTT)
        assert len(segs) == 1
        assert segs[0].text == "Actual content."


class TestParseVttCRLF:
    """Windows CRLF line endings must be handled correctly."""

    def test_crlf_normalised(self):
        vtt = "WEBVTT\r\n\r\n00:00:01.000 --> 00:00:03.000\r\n<v John>Hello.\r\n"
        segs = parse_vtt(vtt)
        assert len(segs) == 1
        assert segs[0].text == "Hello."


class TestParseVttMultiLine:
    """Multi-line cue text (continuation lines after the speaker tag) must be joined."""

    VTT = """\
WEBVTT

00:00:01.000 --> 00:00:06.000
<v John>First part of the sentence,
second part on the next line.
"""

    def test_multiline_joined(self):
        segs = parse_vtt(self.VTT)
        assert len(segs) == 1
        assert "First part" in segs[0].text
        assert "second part" in segs[0].text


class TestParseVttEmptyInput:
    def test_empty_string_returns_empty_list(self):
        assert parse_vtt("") == []

    def test_header_only_returns_empty_list(self):
        assert parse_vtt("WEBVTT\n") == []


class TestParseVttReturnType:
    def test_all_segments_are_vtt_segment(self):
        segs = parse_vtt(SIMPLE_VTT)
        for seg in segs:
            assert isinstance(seg, VttSegment)

    def test_all_fields_populated(self):
        segs = parse_vtt(SIMPLE_VTT)
        for seg in segs:
            assert seg.speaker  # non-empty string
            assert seg.text     # non-empty string
            assert isinstance(seg.start_ms, int)
            assert isinstance(seg.end_ms, int)
